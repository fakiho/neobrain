"""SQLite access layer for the timeline store."""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from . import config
from .models import Event, now_ms
from .schema import SCHEMA, migrate

# PORT-NOTE: schema merged into neobrain/schema.py (single source of truth);
# the old per-file schema.sql pointer is gone. DB filename is neobrain.db
# (set in config) inside the data dir.


def connect(db_path: Optional[Path] = None, readonly: bool = False) -> sqlite3.Connection:
    path = Path(db_path or config.DB_PATH)
    if readonly:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    else:
        config.ensure_dirs()
        conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    # PORT-NOTE: S4 — the full schema is applied, then the additive migration
    # runs (rank DDL + one m_rank row per existing atom + user_version=2), so a
    # fresh store and a legacy v1 store converge on the same shape. The old
    # direct `PRAGMA user_version = 1` stamp is now migrate()'s job.
    conn.executescript(SCHEMA)
    migrate(conn)
    conn.commit()


_INSERT_EVENT = """
INSERT INTO events
  (id, ts, ts_end, source, lane, category, actor, title, detail, status,
   severity, project_id, session_id, refs, raw, ingested_at)
VALUES
  (:id, :ts, :ts_end, :source, :lane, :category, :actor, :title, :detail,
   :status, :severity, :project_id, :session_id, :refs, :raw, :ingested_at)
"""

# UPDATE (not INSERT OR REPLACE) keeps the row's rowid stable: the FTS index is
# keyed to it, and REPLACE would delete+reinsert, churning the key every write.
_UPDATE_EVENT = """
UPDATE events SET
  ts = :ts, ts_end = :ts_end, source = :source, lane = :lane, category = :category,
  actor = :actor, title = :title, detail = :detail, status = :status,
  severity = :severity, project_id = :project_id, session_id = :session_id,
  refs = :refs, raw = :raw, ingested_at = :ingested_at
WHERE id = :id
"""


def upsert_events(conn: sqlite3.Connection, events: Iterable[Event]) -> tuple[int, int]:
    """Insert/update events and keep the FTS index in sync. Returns (added, updated).

    The FTS row is keyed to the ``events`` rowid, because FTS5 deletes are
    log-time on the rowid while the old ``WHERE event_id = ?`` delete had to
    scan the whole index (``event_id`` is UNINDEXED in the FTS5 table) — that
    made every full ingest quadratic, pegging a core for minutes while holding
    the writer lock. ``schema.migrate()`` v3 rebuilds legacy indexes so their
    rowids line up.
    """
    added = updated = 0
    ingested = now_ms()
    cur = conn.cursor()
    for ev in events:
        row = ev.as_row(ingested)
        prev = cur.execute("SELECT rowid FROM events WHERE id = ?", (row["id"],)).fetchone()
        if prev is None:
            cur.execute(_INSERT_EVENT, row)
            rid = cur.lastrowid
            added += 1
        else:
            cur.execute(_UPDATE_EVENT, row)
            rid = prev[0]
            updated += 1
        cur.execute("DELETE FROM events_fts WHERE rowid = ?", (rid,))
        cur.execute(
            "INSERT INTO events_fts(rowid, event_id, title, detail) VALUES (?, ?, ?, ?)",
            (rid, row["id"], row["title"], row["detail"] or ""),
        )
    conn.commit()
    return added, updated


def upsert_links(conn: sqlite3.Connection, links: Iterable[tuple[str, str, str]]) -> int:
    cur = conn.cursor()
    n = 0
    for frm, to, kind in links:
        cur.execute(
            "INSERT OR IGNORE INTO event_links(from_id, to_id, kind) VALUES (?, ?, ?)",
            (frm, to, kind),
        )
        n += cur.rowcount
    conn.commit()
    return n


def upsert_doc_version(
    conn: sqlite3.Connection,
    *,
    vid: str,
    doc_path: str,
    ts: int,
    content_hash: str,
    content: str,
    diff: Optional[str] = None,
) -> bool:
    cur = conn.execute(
        """
        INSERT OR IGNORE INTO doc_versions(id, doc_path, ts, content_hash, content, diff)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (vid, doc_path, ts, content_hash, content, diff),
    )
    conn.commit()
    return cur.rowcount > 0


def upsert_sessions(conn: sqlite3.Connection, sessions: Iterable[dict]) -> int:
    cur = conn.cursor()
    n = 0
    for s in sessions:
        cur.execute(
            """
            INSERT OR REPLACE INTO sessions
              (id, project_id, directory, title, agent, model, cost, ts_created, ts_updated)
            VALUES (:id, :project_id, :directory, :title, :agent, :model, :cost,
                    :ts_created, :ts_updated)
            """,
            s,
        )
        n += 1
    conn.commit()
    return n


def set_ingest_state(conn: sqlite3.Connection, source: str, cursor: str, note: str = "") -> None:
    conn.execute(
        """
        INSERT OR REPLACE INTO ingest_state(source, cursor, updated_at, note)
        VALUES (?, ?, ?, ?)
        """,
        (source, cursor, now_ms(), note),
    )
    conn.commit()


def start_run(conn: sqlite3.Connection, source: str) -> int:
    cur = conn.execute(
        "INSERT INTO ingest_runs(started_at, source) VALUES (?, ?)", (now_ms(), source)
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_run(
    conn: sqlite3.Connection, run_id: int, *, added: int, updated: int, errors: int, note: str = ""
) -> None:
    conn.execute(
        "UPDATE ingest_runs SET finished_at=?, added=?, updated=?, errors=?, note=? WHERE id=?",
        (now_ms(), added, updated, errors, note, run_id),
    )
    conn.commit()


# --- queries ------------------------------------------------------------

def _row_to_dict(row: sqlite3.Row) -> dict:
    d = dict(row)
    for k in ("refs", "raw"):
        if k in d and isinstance(d[k], str):
            try:
                d[k] = json.loads(d[k])
            except json.JSONDecodeError:
                pass
    return d


def events(
    conn: sqlite3.Connection,
    *,
    start: Optional[int] = None,
    end: Optional[int] = None,
    lane: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    project_id: Optional[str] = None,
    session_id: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = 200,
    offset: int = 0,
) -> list[dict]:
    where: list[str] = []
    params: list[Any] = []
    if start is not None:
        where.append("e.ts >= ?")
        params.append(start)
    if end is not None:
        where.append("e.ts <= ?")
        params.append(end)
    for col, val in (("lane", lane), ("source", source), ("category", category),
                     ("project_id", project_id), ("session_id", session_id)):
        if val:
            where.append(f"e.{col} = ?")
            params.append(val)

    def _run(use_fts: bool):
        w = list(where)
        p = list(params)
        join = ""
        if q:
            if use_fts:
                join = "JOIN events_fts f ON f.event_id = e.id"
                w.append("events_fts MATCH ?")
                p.append(q)
            else:
                w.append("(e.title LIKE ? OR e.detail LIKE ?)")
                like = f"%{q}%"
                p.extend([like, like])
        sql = f"SELECT e.* FROM events e {join}"
        if w:
            sql += " WHERE " + " AND ".join(w)
        sql += " ORDER BY e.ts ASC LIMIT ? OFFSET ?"
        return [_row_to_dict(r) for r in conn.execute(sql, p + [limit, offset])]

    if q:
        try:
            return _run(True)
        except sqlite3.OperationalError:
            return _run(False)
    return _run(False)


def get_event(conn: sqlite3.Connection, event_id: str) -> Optional[dict]:
    row = conn.execute("SELECT * FROM events WHERE id = ?", (event_id,)).fetchone()
    return _row_to_dict(row) if row else None


def stats(conn: sqlite3.Connection) -> dict:
    out: dict[str, Any] = {}
    out["total"] = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    out["by_source"] = {
        r[0]: r[1] for r in conn.execute("SELECT source, COUNT(*) FROM events GROUP BY source")
    }
    out["by_lane"] = {
        r[0]: r[1]
        for r in conn.execute("SELECT lane, COUNT(*) FROM events GROUP BY lane ORDER BY 2 DESC")
    }
    out["by_category"] = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT category, COUNT(*) FROM events GROUP BY category ORDER BY 2 DESC LIMIT 30"
        )
    }
    out["by_severity"] = {
        r[0]: r[1] for r in conn.execute("SELECT severity, COUNT(*) FROM events GROUP BY severity")
    }
    row = conn.execute("SELECT MIN(ts), MAX(ts) FROM events").fetchone()
    out["range"] = {"min": row[0], "max": row[1]} if row else {}
    out["doc_versions"] = conn.execute("SELECT COUNT(*) FROM doc_versions").fetchone()[0]
    out["links"] = conn.execute("SELECT COUNT(*) FROM event_links").fetchone()[0]
    out["sessions"] = conn.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
    return out


def chain(conn: sqlite3.Connection, event_id: str) -> dict:
    """Return the immediate 'why' chain around an event."""
    out = conn.execute("SELECT * FROM event_links WHERE from_id = ?", (event_id,)).fetchall()
    inc = conn.execute("SELECT * FROM event_links WHERE to_id = ?", (event_id,)).fetchall()
    return {
        "event_id": event_id,
        "out": [_row_to_dict(r) for r in out],
        "in": [_row_to_dict(r) for r in inc],
    }
