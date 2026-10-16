"""Memory maintenance — dry-run-first, provenance-scoped cleanup.

The only operation here is *source-prefix cleanup*: select every atom whose
``m_atoms.source`` starts with a namespace such as ``icm:`` (memories imported
from another tool), report exactly what removing them would touch, and — only
on explicit confirmation — remove them in one transaction.

Design rules (see ``docs/MEMORY.md``):

* ``plan_source_cleanup`` is read-only and is the default everywhere. It never
  writes, so it is safe on a live store (the CLI opens it read-only).
* ``apply_source_cleanup`` needs a *confirmation token* derived from the exact
  set of atoms the plan selected. If the store changed between plan and apply
  the token no longer matches and nothing is deleted.
* Deletion is a single ``BEGIN IMMEDIATE`` transaction: any failure rolls the
  whole cleanup back. Atoms, their edges, embeddings and rank rows go together;
  hubs are only pruned when asked (``prune_empty_hubs``) and only if the
  cleanup leaves them with no atoms.
* A backup is taken *before* the transaction with SQLite's online backup API
  (``sqlite3.Connection.backup`` — stdlib, portable, WAL-safe) and verified
  with ``PRAGMA integrity_check`` plus an atom count. There was no pre-existing
  backup abstraction in neoBrain; this helper is deliberately small and is the
  only one. A file-backed store is required for it; an in-memory/unnamed
  store can only be cleaned with ``backup=False`` (explicit opt-out).
* Matching is a **case-sensitive literal prefix** (``substr(source,1,n)=?``),
  not SQL ``LIKE``: ``LIKE`` is case-insensitive in SQLite and treats ``%``/``_``
  in the prefix as wildcards, which is the wrong failure mode for a delete.
  For ``icm:`` it selects exactly the rows ``source LIKE 'icm:%'`` would,
  except it will not match ``ICM:…``.

Not touched by design: ``m_feedback`` and ``m_ops`` (append-only history —
``forget`` leaves them too), and any atom whose ``source`` does not start with
the prefix. Feedback/ops rows that point at deleted atoms are counted in the
plan so the dangling references are visible.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .models import now_ms

#: Default provenance namespace this operation was built for.
DEFAULT_PREFIX = "icm:"

#: How many atom ids/labels a plan lists (the full set is always counted and
#: hashed into the confirmation token).
DEFAULT_SAMPLE = 20


class MaintenanceError(RuntimeError):
    """Refused or failed maintenance operation; the store was left unchanged."""


# --- helpers --------------------------------------------------------------


def _check_prefix(prefix: str) -> str:
    # A provenance namespace looks like "icm:" — require the trailing colon so
    # "i" or "" can never select half the store by accident.
    if not isinstance(prefix, str) or len(prefix) < 2 or not prefix.endswith(":"):
        raise MaintenanceError(
            f"source prefix must be a namespace ending in ':' (e.g. {DEFAULT_PREFIX!r}); got {prefix!r}"
        )
    return prefix


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _selected_sql(prefix: str) -> tuple[str, tuple]:
    """SQL fragment (+params) selecting the matched atom ids."""
    return "SELECT a.id FROM m_atoms a WHERE substr(a.source, 1, ?) = ?", (len(prefix), prefix)


def _one(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(conn.execute(sql, params).fetchone()[0])


def _token(prefix: str, ids: list[str], prune_empty_hubs: bool) -> str:
    h = hashlib.sha256()
    h.update(json.dumps([prefix, bool(prune_empty_hubs), sorted(ids)]).encode("utf-8"))
    return f"delete-{len(ids)}-{h.hexdigest()[:12]}"


# --- plan (read-only) -----------------------------------------------------


def plan_source_cleanup(
    conn: sqlite3.Connection,
    prefix: str = DEFAULT_PREFIX,
    *,
    prune_empty_hubs: bool = False,
    sample: int = DEFAULT_SAMPLE,
) -> dict[str, Any]:
    """Report what deleting every atom with ``source`` starting ``prefix`` affects.

    Pure SELECTs — never writes. The returned ``confirm_token`` must be passed
    back to :func:`apply_source_cleanup`.
    """
    prefix = _check_prefix(prefix)
    if not _has_table(conn, "m_atoms"):
        raise MaintenanceError("no m_atoms table: not a neoBrain store")
    sel, sp = _selected_sql(prefix)

    ids = [r[0] for r in conn.execute(f"{sel} ORDER BY a.id", sp)]
    atoms_total = _one(conn, "SELECT COUNT(*) FROM m_atoms")
    samples = [
        {"id": r[0], "label": r[1], "type": r[2], "source": r[3], "hub": r[4]}
        for r in conn.execute(
            "SELECT a.id, a.label, a.type, a.source, a.hub FROM m_atoms a "
            "WHERE substr(a.source, 1, ?) = ? ORDER BY a.created DESC, a.id LIMIT ?",
            (*sp, max(0, sample)),
        )
    ]
    by_source = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT a.source, COUNT(*) FROM m_atoms a WHERE substr(a.source, 1, ?) = ? "
            "GROUP BY a.source ORDER BY 2 DESC, 1",
            sp,
        )
    }
    pinned = _one(
        conn,
        "SELECT COUNT(*) FROM m_atoms a WHERE substr(a.source, 1, ?) = ? "
        "AND ',' || replace(coalesce(a.tags,''),' ','') || ',' LIKE '%,pin,%'",
        sp,
    )

    # Edges touching a selected atom, in either direction.
    edge_total = edge_out = edge_in_external = 0
    edge_by_type: dict[str, int] = {}
    if _has_table(conn, "m_edges"):
        edge_where = f"(e.source IN ({sel}) OR e.target IN ({sel}))"
        ep = sp + sp
        edge_total = _one(conn, f"SELECT COUNT(*) FROM m_edges e WHERE {edge_where}", ep)
        edge_by_type = {
            r[0]: r[1]
            for r in conn.execute(
                f"SELECT e.type, COUNT(*) FROM m_edges e WHERE {edge_where} GROUP BY e.type ORDER BY 2 DESC, 1",
                ep,
            )
        }
        edge_out = _one(conn, f"SELECT COUNT(*) FROM m_edges e WHERE e.source IN ({sel})", sp)
        # Retained atoms pointing at a doomed one (e.g. a kept memory that
        # "supersedes" a deleted one) lose that edge — worth seeing up front.
        edge_in_external = _one(
            conn,
            f"SELECT COUNT(*) FROM m_edges e WHERE e.target IN ({sel}) AND e.source NOT IN ({sel})",
            ep,
        )

    embeddings = ranks = feedback = ops = 0
    if _has_table(conn, "m_embeddings"):
        embeddings = _one(conn, f"SELECT COUNT(*) FROM m_embeddings WHERE atom_id IN ({sel})", sp)
    if _has_table(conn, "m_rank"):
        ranks = _one(conn, f"SELECT COUNT(*) FROM m_rank WHERE atom_id IN ({sel})", sp)
    if _has_table(conn, "m_feedback"):
        feedback = _one(conn, f"SELECT COUNT(*) FROM m_feedback WHERE atom_id IN ({sel})", sp)
    if _has_table(conn, "m_ops"):
        ops = _one(conn, f"SELECT COUNT(*) FROM m_ops WHERE atom_id IN ({sel})", sp)

    # Hubs the selected atoms belong to (by column or 'about' edge) and which of
    # them would be left with no atoms at all once the cleanup is done.
    hub_ids: set[str] = set()
    hub_ids.update(
        r[0] for r in conn.execute(
            "SELECT DISTINCT a.hub FROM m_atoms a WHERE substr(a.source, 1, ?) = ? AND a.hub IS NOT NULL", sp)
    )
    if _has_table(conn, "m_edges"):
        hub_ids.update(
            r[0] for r in conn.execute(
                f"SELECT DISTINCT e.target FROM m_edges e WHERE e.type='about' AND e.source IN ({sel})", sp)
        )
    hubs: list[dict[str, Any]] = []
    for hid in sorted(hub_ids):
        sel_n = _one(
            conn,
            "SELECT COUNT(*) FROM m_atoms a WHERE (a.hub = ? OR a.id IN "
            "(SELECT e.source FROM m_edges e WHERE e.target = ? AND e.type='about')) "
            "AND substr(coalesce(a.source,''), 1, ?) = ?",
            (hid, hid, *sp),
        )
        keep_n = _one(
            conn,
            "SELECT COUNT(*) FROM m_atoms a WHERE (a.hub = ? OR a.id IN "
            "(SELECT e.source FROM m_edges e WHERE e.target = ? AND e.type='about')) "
            "AND substr(coalesce(a.source,''), 1, ?) <> ?",
            (hid, hid, *sp),
        )
        exists = _has_table(conn, "m_hubs") and conn.execute(
            "SELECT 1 FROM m_hubs WHERE id=?", (hid,)
        ).fetchone() is not None
        hubs.append({
            "id": hid,
            "selected_atoms": sel_n,
            "remaining_atoms": keep_n,
            "would_be_empty": keep_n == 0,
            "exists": exists,
        })
    empty = [h["id"] for h in hubs if h["would_be_empty"] and h["exists"]]

    return {
        "operation": "source_cleanup",
        "mode": "plan",
        "source_prefix": prefix,
        "match": "case-sensitive literal prefix on m_atoms.source",
        "atoms": {
            "selected": len(ids),
            "total": atoms_total,
            "remaining": atoms_total - len(ids),
            "pinned_selected": pinned,
            "by_source": by_source,
            "sample": samples,
            "sample_truncated": len(ids) > len(samples),
        },
        "edges": {
            "touched": edge_total,
            "from_selected": edge_out,
            "from_retained_to_selected": edge_in_external,
            "by_type": edge_by_type,
        },
        "dependents": {
            "embeddings": embeddings,
            "rank_rows": ranks,
            "feedback_rows_kept": feedback,
            "ops_rows_kept": ops,
        },
        "hubs": {
            "affected": hubs,
            "empty_after": empty,
            "will_prune": empty if prune_empty_hubs else [],
        },
        "prune_empty_hubs": bool(prune_empty_hubs),
        "confirm_token": _token(prefix, ids, prune_empty_hubs) if ids else None,
        "destructive": False,
        "notes": [
            "m_feedback and m_ops history is kept (as forget() does); rows for deleted atoms become dangling.",
            "Hubs are only deleted with prune_empty_hubs and only when no atom remains in them.",
        ],
    }


# --- backup ---------------------------------------------------------------


def _db_file(conn: sqlite3.Connection) -> Path | None:
    for _seq, name, file in conn.execute("PRAGMA database_list"):
        if name == "main":
            return Path(file) if file else None
    return None


def backup_database(conn: sqlite3.Connection, dest_dir: Path | None = None,
                    label: str = "pre-cleanup") -> dict[str, Any]:
    """Write a verified, consistent copy of the open store via SQLite's backup API.

    Returns ``{"path", "atoms", "integrity"}``. Raises :class:`MaintenanceError`
    for an in-memory store, an unwritable target, or a copy that fails
    verification (the partial file is removed). It never overwrites a file.
    """
    src = _db_file(conn)
    if src is None:
        raise MaintenanceError("store is not file-backed; cannot back it up (pass backup=False to proceed without)")
    if conn.in_transaction:
        raise MaintenanceError("refusing to back up with a transaction open on the connection")
    target_dir = Path(dest_dir) if dest_dir is not None else src.parent / "backups"
    target_dir.mkdir(parents=True, exist_ok=True)
    path = target_dir / f"{src.stem}-{label}-{now_ms()}.db"
    if path.exists():
        raise MaintenanceError(f"backup target already exists: {path}")
    dst = sqlite3.connect(str(path))
    try:
        conn.backup(dst)
        dst.commit()
        integrity = dst.execute("PRAGMA integrity_check").fetchone()[0]
        copied = _one(dst, "SELECT COUNT(*) FROM m_atoms") if _has_table(dst, "m_atoms") else 0
    except sqlite3.Error as exc:
        dst.close()
        path.unlink(missing_ok=True)
        raise MaintenanceError(f"backup failed: {exc}") from exc
    dst.close()
    live = _one(conn, "SELECT COUNT(*) FROM m_atoms") if _has_table(conn, "m_atoms") else 0
    if integrity != "ok" or copied != live:
        path.unlink(missing_ok=True)
        raise MaintenanceError(
            f"backup verification failed (integrity={integrity!r}, atoms {copied} != {live})"
        )
    return {"path": str(path), "atoms": copied, "integrity": integrity}


# --- apply (destructive) --------------------------------------------------


def apply_source_cleanup(
    conn: sqlite3.Connection,
    prefix: str = DEFAULT_PREFIX,
    *,
    confirm: str | None,
    prune_empty_hubs: bool = False,
    backup: bool = True,
    backup_dir: Path | None = None,
) -> dict[str, Any]:
    """Delete the atoms selected by :func:`plan_source_cleanup`, atomically.

    ``confirm`` must equal the plan's ``confirm_token`` (re-computed here from
    the current store, so a stale token is refused). Raises
    :class:`MaintenanceError` without modifying anything when the token is
    missing/stale, nothing matches, or the backup cannot be made. Any error
    during deletion rolls the entire transaction back.
    """
    prefix = _check_prefix(prefix)
    if conn.in_transaction:
        raise MaintenanceError("refusing to run with a transaction already open on the connection")
    plan = plan_source_cleanup(conn, prefix, prune_empty_hubs=prune_empty_hubs)
    if not plan["atoms"]["selected"]:
        raise MaintenanceError(f"nothing to do: no atoms with source starting {prefix!r}")
    if not confirm or confirm != plan["confirm_token"]:
        raise MaintenanceError(
            "confirmation token missing or stale; re-run the dry-run and pass its confirm_token "
            f"(expected {plan['confirm_token']!r})"
        )

    backup_info: dict[str, Any] | None = None
    if backup:
        backup_info = backup_database(conn, backup_dir)

    sel, sp = _selected_sql(prefix)
    deleted: dict[str, int] = {}
    try:
        conn.execute("BEGIN IMMEDIATE")
        # Re-verify under the write lock: nothing may have changed since the plan.
        again = plan_source_cleanup(conn, prefix, prune_empty_hubs=prune_empty_hubs)
        if again["confirm_token"] != confirm:
            raise MaintenanceError("store changed between plan and apply; nothing deleted")
        empty = list(again["hubs"]["will_prune"])

        # Dependents first, then the atoms themselves.
        if _has_table(conn, "m_edges"):
            deleted["edges"] = conn.execute(
                f"DELETE FROM m_edges WHERE source IN ({sel}) OR target IN ({sel})", sp + sp
            ).rowcount
        if _has_table(conn, "m_embeddings"):
            deleted["embeddings"] = conn.execute(
                f"DELETE FROM m_embeddings WHERE atom_id IN ({sel})", sp
            ).rowcount
        if _has_table(conn, "m_rank"):
            deleted["rank_rows"] = conn.execute(
                f"DELETE FROM m_rank WHERE atom_id IN ({sel})", sp
            ).rowcount
        deleted["atoms"] = conn.execute(
            "DELETE FROM m_atoms WHERE substr(source, 1, ?) = ?", sp
        ).rowcount
        deleted["hubs"] = 0
        for hid in empty:
            deleted["hubs"] += conn.execute("DELETE FROM m_hubs WHERE id=?", (hid,)).rowcount
        if deleted["atoms"] != plan["atoms"]["selected"]:
            raise MaintenanceError(
                f"deleted {deleted['atoms']} atoms but the plan selected {plan['atoms']['selected']}"
            )
        if _has_table(conn, "m_ops"):
            conn.execute(
                "INSERT INTO m_ops(ts, op, atom_id, label, query, session_id) VALUES(?,?,?,?,?,?)",
                (now_ms(), "maintenance", None, f"source_cleanup {prefix} atoms={deleted['atoms']}",
                 None, "maintenance"),
            )
        conn.commit()
    except BaseException:
        if conn.in_transaction:
            conn.rollback()
        raise

    return {
        "operation": "source_cleanup",
        "mode": "applied",
        "source_prefix": prefix,
        "deleted": deleted,
        "backup": backup_info,
        "plan": plan,
        "destructive": True,
    }
