"""Append-only atom history (``m_history``).

Explicit writes, deletes, and both explicit and inferred link events are
recorded in the *same transaction* as the change itself, so the audit trail
cannot diverge from the store. Link rows use reason ``explicit`` or
``inferred``. Text edits and rank changes are not recorded. Rows are never
updated or deleted, and they outlive the atom they describe (there is no
foreign key to ``m_atoms``).
"""
from __future__ import annotations

import sqlite3

from .models import now_ms

#: Events written today: explicit writes (``add``), deletes (``delete``), and
#: both explicit and inferred link events (``link``; reason ``explicit`` or
#: ``inferred``). Text edits and rank changes are not in this set. The column
#: is free text so later events can be added without a migration.
EVENTS = ("add", "delete", "link")


def record_history(conn: sqlite3.Connection, atom_id: str, event: str, *,
                   old_text: str | None = None, new_text: str | None = None,
                   actor: str | None = None, reason: str | None = None,
                   ts: int | None = None) -> None:
    """Insert one history row. Does NOT commit: the caller owns the transaction."""
    conn.execute(
        "INSERT INTO m_history(ts,atom_id,event,old_text,new_text,actor,reason) "
        "VALUES(?,?,?,?,?,?,?)",
        (ts if ts is not None else now_ms(), atom_id, event, old_text, new_text, actor, reason),
    )


def read_history(conn: sqlite3.Connection, atom_id: str) -> list[dict]:
    """The atom's history, oldest first (insertion order breaks timestamp ties)."""
    if not conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='m_history'"
    ).fetchone():
        return []
    return [
        {"id": r[0], "ts": r[1], "atom_id": r[2], "event": r[3],
         "old_text": r[4], "new_text": r[5], "actor": r[6], "reason": r[7]}
        for r in conn.execute(
            "SELECT id,ts,atom_id,event,old_text,new_text,actor,reason "
            "FROM m_history WHERE atom_id=? ORDER BY ts, id", (atom_id,))
    ]
