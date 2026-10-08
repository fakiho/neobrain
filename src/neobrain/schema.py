"""Authoritative SQLite schema for the neoBrain store.

Single source of truth: the old repo split its DDL across ``app/schema.sql``
(events/ingest tables) and ``SCHEMA`` constants inside ``app/mind.py`` and
``app/embeddings.py``. All three are merged here, byte-for-byte as they were,
plus the new migration floor: ``PRAGMA user_version`` (the old repo had no
migration framework).

All DDL uses ``IF NOT EXISTS``, so re-applying the full schema (at init or
lazily from any module) is idempotent.
"""
from __future__ import annotations

import re
import sqlite3

from .models import now_ms

# Schema version floor. v1 = merged legacy schema (S1). v2 adds the rank /
# forgetting store (S4: ``m_rank`` counters + active/ignored/archived state,
# SPEC §4.2/§4.3) as an *additive* migration. v3 rebuilds ``events_fts`` so its
# rowids match ``events.rowid`` — the index is now addressed by rowid, because
# deleting by the UNINDEXED ``event_id`` column scanned the whole index and
# made a full ingest quadratic (see ``db.upsert_events``). v4 links a memory to
# the session it was saved from: ``m_atoms.session_id`` (origin) and
# ``m_feedback.origin_session_id`` (so a rating records both the rater and the
# memory's home session).
USER_VERSION = 4

#: Origin session ids embedded in legacy ``m_atoms.source`` strings
#: ("session ses_…", "session:ses_…", …). Only real ``ses_`` ids are matched;
#: free-text pseudo-sessions like "session:embedding-switch" are left alone.
_SESSION_RE = re.compile(r"ses_[A-Za-z0-9]+")

SCHEMA = """
-- Timeline store schema
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS events (
  id           TEXT PRIMARY KEY,      -- sha1(source|source_ref)
  ts           INTEGER NOT NULL,      -- epoch ms UTC
  ts_end       INTEGER,
  source       TEXT NOT NULL,         -- opencode | git | docs
  lane         TEXT NOT NULL,         -- decision|action|file|research|question|request|git|doc|dream|notice
  category     TEXT NOT NULL,         -- shell|edit|write|read|webfetch|commit|memory|infra|identity|dream|...
  actor        TEXT NOT NULL,         -- agent:<model> | user | git:<author>
  title        TEXT NOT NULL,
  detail       TEXT,
  status       TEXT,
  severity     TEXT,                  -- info|notice|success|warning|error
  project_id   TEXT,
  session_id   TEXT,
  refs         TEXT NOT NULL DEFAULT '{}',   -- JSON
  raw          TEXT,                  -- JSON (drawer / re-derive)
  ingested_at  INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_ts        ON events(ts);
CREATE INDEX IF NOT EXISTS idx_events_lane_ts   ON events(lane, ts);
CREATE INDEX IF NOT EXISTS idx_events_source_ts ON events(source, ts);
CREATE INDEX IF NOT EXISTS idx_events_session   ON events(session_id, ts);
CREATE INDEX IF NOT EXISTS idx_events_project   ON events(project_id, ts);

-- standalone FTS index maintained by the app
CREATE VIRTUAL TABLE IF NOT EXISTS events_fts USING fts5(event_id UNINDEXED, title, detail);

-- content snapshots so doc history survives even without git
CREATE TABLE IF NOT EXISTS doc_versions (
  id           TEXT PRIMARY KEY,      -- sha1(path|content_hash)
  doc_path     TEXT NOT NULL,
  ts           INTEGER NOT NULL,
  content_hash TEXT NOT NULL,
  content      TEXT NOT NULL,
  diff         TEXT
);
CREATE INDEX IF NOT EXISTS idx_docver_path_ts ON doc_versions(doc_path, ts);

-- decision <-> evidence links (the "why" chain)
CREATE TABLE IF NOT EXISTS event_links (
  from_id TEXT NOT NULL,
  to_id   TEXT NOT NULL,
  kind    TEXT NOT NULL,              -- rationale|evidence|implements|supersedes
  PRIMARY KEY (from_id, to_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_links_from ON event_links(from_id);

-- nightly/local LLM output
CREATE TABLE IF NOT EXISTS enrichment (
  event_id   TEXT PRIMARY KEY,
  summary    TEXT,
  decision   TEXT,
  tags       TEXT,
  model      TEXT,
  created_at INTEGER
);

-- sessions (workstreams), denormalised for fast filtering
CREATE TABLE IF NOT EXISTS sessions (
  id           TEXT PRIMARY KEY,
  project_id   TEXT,
  directory    TEXT,
  title        TEXT,
  agent        TEXT,
  model        TEXT,
  cost         REAL,
  ts_created   INTEGER,
  ts_updated   INTEGER
);

-- per-source ingestion bookkeeping
CREATE TABLE IF NOT EXISTS ingest_state (
  source     TEXT PRIMARY KEY,
  cursor     TEXT,
  updated_at INTEGER,
  note       TEXT
);

-- machine-readable run log
CREATE TABLE IF NOT EXISTS ingest_runs (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  started_at  INTEGER NOT NULL,
  finished_at INTEGER,
  source      TEXT,
  added       INTEGER DEFAULT 0,
  updated     INTEGER DEFAULT 0,
  errors      INTEGER DEFAULT 0,
  note        TEXT
);

CREATE TABLE IF NOT EXISTS m_atoms (
  id TEXT PRIMARY KEY, label TEXT NOT NULL, type TEXT NOT NULL, created INTEGER NOT NULL,
  text TEXT, source TEXT, tags TEXT, weight REAL DEFAULT 0.5, hub TEXT, hash TEXT,
  session_id TEXT
);
CREATE TABLE IF NOT EXISTS m_hubs (id TEXT PRIMARY KEY, label TEXT NOT NULL, created INTEGER);
CREATE TABLE IF NOT EXISTS m_edges (
  source TEXT NOT NULL, target TEXT NOT NULL, type TEXT NOT NULL,
  PRIMARY KEY (source, target, type)
);
CREATE INDEX IF NOT EXISTS idx_m_atoms_ts ON m_atoms(created);
CREATE INDEX IF NOT EXISTS idx_m_atoms_hub ON m_atoms(hub);
CREATE INDEX IF NOT EXISTS idx_m_edges_src ON m_edges(source);
CREATE TABLE IF NOT EXISTS m_ops (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, op TEXT NOT NULL,
  atom_id TEXT, label TEXT, query TEXT, session_id TEXT
);
CREATE TABLE IF NOT EXISTS m_feedback (
  id INTEGER PRIMARY KEY AUTOINCREMENT, ts INTEGER NOT NULL, atom_id TEXT NOT NULL,
  signal TEXT NOT NULL, source TEXT, session_id TEXT, origin_session_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_m_feedback_atom ON m_feedback(atom_id);

CREATE TABLE IF NOT EXISTS m_embeddings (
  atom_id TEXT PRIMARY KEY,
  model TEXT NOT NULL,
  dim INTEGER NOT NULL,
  hash TEXT NOT NULL,
  vec BLOB NOT NULL,
  updated INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m_embeddings_model ON m_embeddings(model);
"""


# --- S4: rank + forgetting (additive v2) --------------------------------
#
# One row per atom: the deterministic quality score, the exposure counters
# (``served`` on every recall, ``interacted`` on every feedback ping) and the
# forgetting state machine. Kept out of ``m_atoms`` so the ranked counters can
# evolve without touching the atom row. ``atom_id`` references ``m_atoms(id)``
# with ``ON DELETE CASCADE``: foreign keys are ON, so without the cascade the
# existing delete paths (``forget`` and the doc rebuild in ``import_mind``)
# would fail on a ranked atom.
RANK_DDL = """
CREATE TABLE IF NOT EXISTS m_rank (
  atom_id     TEXT PRIMARY KEY REFERENCES m_atoms(id) ON DELETE CASCADE,
  quality     REAL NOT NULL DEFAULT 0.5,
  served      INTEGER NOT NULL DEFAULT 0,
  interacted  INTEGER NOT NULL DEFAULT 0,
  last_served INTEGER,
  state       TEXT NOT NULL DEFAULT 'active',
  state_since INTEGER,
  updated     INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_m_rank_state ON m_rank(state);
"""

SCHEMA = SCHEMA + RANK_DDL


# --- S6c: FTS rowid alignment (additive v3) ------------------------------
#
# Deletes in ``events_fts`` are addressed by rowid (log-time). The legacy
# index matched on ``event_id``, which is UNINDEXED in FTS5, so each delete
# scanned the whole index — quadratic over an ingest. One rebuild lines the
# FTS rowids up with ``events.rowid`` (and drops any orphaned/stale rows);
# ``db.upsert_events`` keeps them aligned from then on.
FTS_REBUILD = """
DELETE FROM events_fts;
INSERT INTO events_fts(rowid, event_id, title, detail)
  SELECT rowid, id, title, COALESCE(detail, '') FROM events;
"""


def apply(conn: sqlite3.Connection) -> None:
    """Apply the full schema and run data migrations to ``USER_VERSION``.

    Idempotent: the DDL is all ``CREATE ... IF NOT EXISTS`` and ``migrate``
    early-returns once the store is stamped.
    """
    conn.executescript(SCHEMA)
    migrate(conn)


def migrate(conn: sqlite3.Connection, now: int | None = None) -> int:
    """Bring a store up to ``USER_VERSION`` (additive; never destructive).

    v1 -> v2 applies the rank DDL and backfills one ``m_rank`` row per existing
    atom (quality 0.5, ``active``, zero counters). v2 -> v3 rebuilds the FTS
    index so its rowids match ``events.rowid`` (fixing the quadratic ingest).
    v3 -> v4 adds the session-provenance columns (``m_atoms.session_id``,
    ``m_feedback.origin_session_id``) and backfills the origin session from any
    ``ses_`` id embedded in ``m_atoms.source``.
    A fresh/unmigrated store is completed with the full schema first.
    Idempotent: once stamped, later calls are a no-op. Returns the version.
    """
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m_atoms'"
    ).fetchone():
        conn.executescript(SCHEMA)  # no floor table yet: build it first
    # Column additions are idempotent and run before the version gate, so a
    # store already stamped at an older version still gains the new columns.
    _ensure_columns(conn)
    version = user_version(conn)
    if version >= USER_VERSION:
        return version
    conn.executescript(RANK_DDL)
    if version < 2:
        ts = now if now is not None else now_ms()
        conn.execute(
            """INSERT OR IGNORE INTO m_rank
                 (atom_id, quality, served, interacted, last_served, state, state_since, updated)
               SELECT id, 0.5, 0, 0, NULL, 'active', NULL, ? FROM m_atoms""",
            (ts,),
        )
    if version < 3:
        conn.executescript(FTS_REBUILD)
    if version < 4:
        _backfill_origin_sessions(conn)
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")
    conn.commit()
    return USER_VERSION


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    """The column names currently present on ``table``."""
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}


def _ensure_columns(conn: sqlite3.Connection) -> None:
    """Add the v4 provenance columns to a pre-v4 store (no-op once present)."""
    if "session_id" not in _columns(conn, "m_atoms"):
        conn.execute("ALTER TABLE m_atoms ADD COLUMN session_id TEXT")
    if "origin_session_id" not in _columns(conn, "m_feedback"):
        conn.execute("ALTER TABLE m_feedback ADD COLUMN origin_session_id TEXT")


def _backfill_origin_sessions(conn: sqlite3.Connection) -> None:
    """Recover ``m_atoms.session_id`` from legacy ``source`` text, once.

    Only atoms still missing a session are touched, and only when ``source``
    contains a real ``ses_`` id — so a re-run is a no-op and pseudo-sessions
    ("session:embedding-switch") are never mistaken for one.
    """
    rows = conn.execute(
        "SELECT id, source FROM m_atoms WHERE session_id IS NULL AND source IS NOT NULL"
    ).fetchall()
    for atom_id, source in rows:
        m = _SESSION_RE.search(source or "")
        if m:
            conn.execute("UPDATE m_atoms SET session_id=? WHERE id=?", (m.group(0), atom_id))


def user_version(conn: sqlite3.Connection) -> int:
    """The store's current schema version (0 = fresh/unmigrated database)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
