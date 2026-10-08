"""Additive migrations (v1 -> v2 rank backfill, v2 -> v3 FTS rowid rebuild)."""
from __future__ import annotations

import sqlite3
from pathlib import Path

from neobrain import db, schema


def _connect(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def _insert_atom(conn: sqlite3.Connection, atom_id: str) -> None:
    conn.execute(
        "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (atom_id, "label " + atom_id, "observation", 1, "text", "test", "agent", 0.5, "agent", "h"),
    )


def test_v1_to_v4_backfills_rank_rows(tmp_path):
    conn = _connect(tmp_path / "v1.db")
    conn.executescript(schema.SCHEMA)          # current (v4) schema...
    conn.execute("DROP TABLE m_rank")          # ...rewound to a v1 store
    conn.execute("PRAGMA user_version = 1")
    _insert_atom(conn, "a1")
    _insert_atom(conn, "a2")
    conn.commit()
    assert schema.user_version(conn) == 1

    assert schema.migrate(conn, now=123) == 4
    assert schema.user_version(conn) == 4

    rows = list(conn.execute(
        "SELECT atom_id,quality,served,interacted,last_served,state,state_since,updated "
        "FROM m_rank ORDER BY atom_id"))
    assert [r["atom_id"] for r in rows] == ["a1", "a2"]
    for row in rows:
        assert row["quality"] == 0.5
        assert row["served"] == 0 and row["interacted"] == 0
        assert row["last_served"] is None and row["state_since"] is None
        assert row["state"] == "active"
        assert row["updated"] == 123
    # existing data intact; second migrate is a no-op
    assert conn.execute("SELECT COUNT(*) FROM m_atoms").fetchone()[0] == 2
    assert schema.migrate(conn, now=999) == 4
    assert conn.execute("SELECT COUNT(*) FROM m_rank").fetchone()[0] == 2
    conn.close()


def test_migrate_preserves_existing_rank_state(tmp_path):
    conn = _connect(tmp_path / "keep.db")
    conn.executescript(schema.SCHEMA)
    _insert_atom(conn, "a1")
    conn.execute(
        "INSERT INTO m_rank(atom_id,quality,served,interacted,last_served,state,state_since,updated) "
        "VALUES('a1',0.9,7,2,555,'ignored',555,600)"
    )
    conn.execute("PRAGMA user_version = 1")
    conn.commit()

    schema.migrate(conn, now=1)
    row = conn.execute(
        "SELECT quality,served,interacted,state FROM m_rank WHERE atom_id='a1'").fetchone()
    assert tuple(row) == (0.9, 7, 2, "ignored")  # INSERT OR IGNORE, not reset
    conn.close()


def test_fresh_init_is_v4(tmp_path, monkeypatch):
    from neobrain import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    conn = db.connect(tmp_path / "fresh.db")
    db.init_db(conn)
    assert schema.user_version(conn) == 4
    assert schema.USER_VERSION == 4
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m_rank'").fetchone() is not None
    conn.close()


def test_v3_rebuild_realigns_fts_rowids(tmp_path):
    """A legacy index (wrong rowids, stale rows) is rebuilt to match events."""
    conn = _connect(tmp_path / "fts.db")
    conn.executescript(schema.SCHEMA)
    conn.execute("PRAGMA user_version = 2")  # legacy store, index not aligned
    conn.execute(
        "INSERT INTO events(id,ts,source,lane,category,actor,title,detail,ingested_at) "
        "VALUES('a',1,'git','action','commit','git:x','hello','world',1)")
    conn.execute("INSERT INTO events_fts(rowid,event_id,title,detail) VALUES(99,'a','hello','world')")
    conn.execute("INSERT INTO events_fts(rowid,event_id,title,detail) VALUES(500,'ghost','stale','stale')")
    conn.commit()

    assert schema.migrate(conn) == 4

    rows = list(conn.execute("SELECT rowid, event_id FROM events_fts"))
    assert len(rows) == 1 and rows[0]["event_id"] == "a"     # stale row gone
    rid = conn.execute("SELECT rowid FROM events WHERE id='a'").fetchone()[0]
    assert rows[0]["rowid"] == rid                           # aligned
    conn.close()


def test_upsert_events_keeps_fts_in_sync(tmp_path):
    """Re-upserting updates the FTS row in place — no duplicates, new text found."""
    conn = db.connect(tmp_path / "sync.db")
    db.init_db(conn)
    from neobrain.models import Event

    ev = Event(ts=1, source="git", lane="action", category="commit", actor="git:x",
               title="first title", id="e1", detail="first detail")
    assert db.upsert_events(conn, [ev]) == (1, 0)
    rid = conn.execute("SELECT rowid FROM events WHERE id='e1'").fetchone()[0]

    ev.title, ev.detail = "second title", "second detail"
    assert db.upsert_events(conn, [ev]) == (0, 1)

    assert conn.execute("SELECT COUNT(*) FROM events_fts").fetchone()[0] == 1
    assert conn.execute("SELECT rowid FROM events_fts").fetchone()[0] == rid
    assert conn.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'second'").fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM events_fts WHERE events_fts MATCH 'first'").fetchone()[0] == 0
    conn.close()


def test_rank_row_cascades_with_atom_delete(tmp_path):
    """forget()/import_mind() delete atoms; the FK must not block them."""
    conn = _connect(tmp_path / "cascade.db")
    db.init_db(conn)
    _insert_atom(conn, "a1")
    conn.execute(
        "INSERT INTO m_rank(atom_id,quality,served,interacted,state,updated) "
        "VALUES('a1',0.5,1,0,'active',1)")
    conn.commit()
    conn.execute("DELETE FROM m_atoms WHERE id='a1'")  # would raise without ON DELETE CASCADE
    assert conn.execute("SELECT COUNT(*) FROM m_rank WHERE atom_id='a1'").fetchone()[0] == 0
    conn.close()


def test_v4_adds_provenance_columns_to_pre_v4_store(tmp_path):
    """A store built before v4 gains both columns via ALTER, not just the schema."""
    conn = _connect(tmp_path / "pre4.db")
    conn.executescript(
        "CREATE TABLE m_atoms (id TEXT PRIMARY KEY, label TEXT NOT NULL, type TEXT NOT NULL, "
        "created INTEGER NOT NULL, text TEXT, source TEXT, tags TEXT, weight REAL, hub TEXT, hash TEXT);"
        "CREATE TABLE m_feedback (id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, "
        "atom_id TEXT NOT NULL, signal TEXT NOT NULL, source TEXT, session_id TEXT);"
    )
    conn.execute("PRAGMA user_version = 3")
    conn.commit()
    assert "session_id" not in {r[1] for r in conn.execute("PRAGMA table_info(m_atoms)")}

    assert schema.migrate(conn) == 4
    assert "session_id" in {r[1] for r in conn.execute("PRAGMA table_info(m_atoms)")}
    assert "origin_session_id" in {r[1] for r in conn.execute("PRAGMA table_info(m_feedback)")}
    conn.close()


def test_v4_backfills_origin_session_from_source(tmp_path):
    """Legacy atoms gain m_atoms.session_id from a `ses_` id in source; pseudo-sessions don't."""
    conn = _connect(tmp_path / "v4.db")
    conn.executescript(schema.SCHEMA)          # current schema, then rewind the stamp
    conn.execute("PRAGMA user_version = 3")
    for aid, src in (
        ("a1", "session ses_abc123XYZ"),
        ("a2", "session:ses_def456"),
        ("a3", "session:embedding-switch"),
        ("a4", None),
    ):
        conn.execute(
            "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (aid, aid, "observation", 1, "t", src, "agent", 0.5, "agent", "h" + aid),
        )
    conn.commit()

    assert schema.migrate(conn) == 4
    got = dict(conn.execute("SELECT id, session_id FROM m_atoms"))
    assert got["a1"] == "ses_abc123XYZ"
    assert got["a2"] == "ses_def456"
    assert got["a3"] is None      # pseudo-session, not a real id
    assert got["a4"] is None
    # idempotent: re-running the migration changes nothing
    schema.migrate(conn)
    assert dict(conn.execute("SELECT id, session_id FROM m_atoms")) == got
    conn.close()
