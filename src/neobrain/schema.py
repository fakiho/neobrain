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

import sqlite3

# Schema version floor. S1 ships the merged legacy schema as version 1.
USER_VERSION = 1

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
  text TEXT, source TEXT, tags TEXT, weight REAL DEFAULT 0.5, hub TEXT, hash TEXT
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
  signal TEXT NOT NULL, source TEXT, session_id TEXT
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


def apply(conn: sqlite3.Connection) -> None:
    """Apply the full schema and stamp the migration floor version.

    Idempotent: every statement is ``CREATE ... IF NOT EXISTS`` and the
    ``user_version`` write is a no-op once set to the same value.
    """
    conn.executescript(SCHEMA)
    conn.execute(f"PRAGMA user_version = {USER_VERSION}")


def user_version(conn: sqlite3.Connection) -> int:
    """The store's current schema version (0 = fresh/unmigrated database)."""
    return int(conn.execute("PRAGMA user_version").fetchone()[0])
