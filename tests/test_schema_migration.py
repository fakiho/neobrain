"""v1 -> v2 additive migration (m_rank backfill) and schema guarantees."""
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


def test_v1_to_v2_backfills_rank_rows(tmp_path):
    conn = _connect(tmp_path / "v1.db")
    conn.executescript(schema.SCHEMA)          # current (v2) schema...
    conn.execute("DROP TABLE m_rank")          # ...rewound to a v1 store
    conn.execute("PRAGMA user_version = 1")
    _insert_atom(conn, "a1")
    _insert_atom(conn, "a2")
    conn.commit()
    assert schema.user_version(conn) == 1

    assert schema.migrate(conn, now=123) == 2
    assert schema.user_version(conn) == 2

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
    assert schema.migrate(conn, now=999) == 2
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


def test_fresh_init_is_v2(tmp_path, monkeypatch):
    from neobrain import config

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    conn = db.connect(tmp_path / "fresh.db")
    db.init_db(conn)
    assert schema.user_version(conn) == 2
    assert schema.USER_VERSION == 2
    assert conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m_rank'").fetchone() is not None
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
