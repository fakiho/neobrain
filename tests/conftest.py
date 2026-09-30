"""Shared test fixtures.

Tests must be fast and hermetic: no network, no writes outside ``tmp_path``.
Embeddings are disabled globally, and the rank knobs are pinned to their
documented defaults so an ambient ``NEOBRAIN_RANK_*`` environment cannot make
the scores non-deterministic.
"""
from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from neobrain import config, db  # noqa: E402

# Never let a test reach Ollama / the LiteLLM gateway.
config.EMBED_ENABLED = False

_RANK_DEFAULTS = {
    "rank_decay_floor": 0.35,
    "rank_conn_floor": 0.5,
    "rank_half_life_days": 21.0,
    "rank_prior_n": 10.0,
    "rank_ignore_below": 0.15,
    "rank_archive_exposure": 5,
}


@pytest.fixture
def conn(tmp_path, monkeypatch):
    """A fresh, migrated store in ``tmp_path`` (never the repo data dir)."""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    for key, value in _RANK_DEFAULTS.items():
        monkeypatch.setattr(config.settings, key, value)
    connection = db.connect(tmp_path / "neobrain.db")
    db.init_db(connection)
    yield connection
    connection.close()


@pytest.fixture
def add_atom(conn):
    """Insert a minimal atom + hub edge directly (no rank row, no embeddings)."""

    def _add(
        atom_id: str,
        *,
        created: int,
        label: str | None = None,
        text: str | None = None,
        hub: str = "agent",
        weight: float = 0.5,
        atype: str = "observation",
    ) -> str:
        conn.execute(
            "INSERT INTO m_atoms(id,label,type,created,text,source,tags,weight,hub,hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (atom_id, label or f"label {atom_id}", atype, created, text or f"body {atom_id}",
             "test", hub, weight, hub, "hash-" + atom_id),
        )
        conn.execute("INSERT OR IGNORE INTO m_hubs(id,label,created) VALUES(?,?,?)", (hub, hub, created))
        conn.execute(
            "INSERT OR IGNORE INTO m_edges(source,target,type) VALUES(?,?,?)", (atom_id, hub, "about")
        )
        conn.commit()
        return atom_id

    return _add


def raw_connect(path: Path) -> sqlite3.Connection:
    """A raw connection with the same pragmas as ``db.connect`` (no dir side effects)."""
    connection = sqlite3.connect(str(path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 20000")
    return connection
