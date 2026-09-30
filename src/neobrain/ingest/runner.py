"""Ingest orchestration: run adapters, upsert, record runs and links.

PORT-NOTE: verbatim logic port of ``timeline/app/ingest/runner.py`` with three
deviations, each marked inline:

* imports use the ``neobrain.*`` package;
* each adapter call is wrapped so a broken source is counted in the per-source
  ``errors`` budget and can never abort the other sources (the S6b contract
  exposes per-source ``errors``; the old code had no error handling);
* ``run_all()`` is the new public entry point (``{source: {added, updated,
  errors}}``) used by the life loop; ``run()`` keeps the old rich summary.

Both functions record one ``ingest_runs`` row per source, as the old code did,
and both run the same post-processing: ``mind.import_mind``, a best-effort
embedding top-up and the periodic (≥20 h) consolidation.
"""
from __future__ import annotations

import sqlite3
import threading
from typing import Iterable

from .. import config, db, embeddings, mind
from ..models import Event, event_id, now_ms
from . import docs as docs_adapter
from . import gitrepo, opencode

DEFAULT_SOURCES = ("opencode", "git", "docs")

# One ingest at a time. The life loop and the one-shot API/CLI trigger share
# this process; the old app's single scheduler thread never had to guard this,
# and two concurrent full re-scans saturate the box and starve every reader.
_run_lock = threading.Lock()

_SKIPPED = {
    "events_total": 0,
    "added": 0,
    "updated": 0,
    "links": 0,
    "sessions": 0,
    "doc_versions": 0,
    "per_source": {},
    "by_source": {},
    "mind": None,
    "embeddings": None,
    "dream": None,
    "skipped": "already running",
}


def _existing_event_ids(conn: sqlite3.Connection, ids: Iterable[str]) -> set[str]:
    """Ids already present in ``events`` (one query per 400-id chunk)."""
    found: set[str] = set()
    id_list = list(ids)
    for i in range(0, len(id_list), 400):
        chunk = id_list[i : i + 400]
        placeholders = ",".join("?" * len(chunk))
        found.update(
            r[0] for r in conn.execute(f"SELECT id FROM events WHERE id IN ({placeholders})", chunk)
        )
    return found


def run(conn: sqlite3.Connection, sources: Iterable[str] = DEFAULT_SOURCES) -> dict:
    """One-shot entry: skips (never queues) when another run is in flight."""
    if not _run_lock.acquire(blocking=False):
        return dict(_SKIPPED)
    try:
        return _run_locked(conn, sources)
    finally:
        _run_lock.release()


def _run_locked(conn: sqlite3.Connection, sources: Iterable[str] = DEFAULT_SOURCES) -> dict:
    sources = tuple(sources)
    all_events: list[Event] = []
    link_intents: list[tuple[str, str, str]] = []
    sessions: list[dict] = []
    versions: list[dict] = []
    opencode_events: list[Event] = []
    per_source: dict[str, int] = {}
    errors: dict[str, int] = {}

    if "opencode" in sources:
        try:
            evs, meta = opencode.iter_events()
            opencode_events = evs
            sessions = meta.get("sessions", [])
            for frm, to, kind in meta.get("links", []):
                link_intents.append(
                    (event_id("opencode", frm), event_id("opencode", to), kind)
                )
            all_events.extend(evs)
            per_source["opencode"] = len(evs)
            errors["opencode"] = 0
        except Exception:  # noqa: BLE001 - a broken source must not sink the run
            per_source["opencode"] = 0
            errors["opencode"] = 1

    if "docs" in sources:
        try:
            devs, dvers = docs_adapter.iter_events()
            all_events.extend(devs)
            versions.extend(dvers)
            if opencode_events:
                versions.extend(docs_adapter.versions_from_writes(opencode_events))
            per_source["docs"] = len(devs)
            errors["docs"] = 0
        except Exception:  # noqa: BLE001
            per_source["docs"] = 0
            errors["docs"] = 1

    if "git" in sources:
        try:
            gev = gitrepo.iter_events()
            all_events.extend(gev)
            per_source["git"] = len(gev)
            errors["git"] = 0
        except Exception:  # noqa: BLE001
            per_source["git"] = 0
            errors["git"] = 1

    # Per-source added/updated, computed against the pre-upsert store so the
    # ingest_runs rows and the run_all contract report real numbers.
    finalized = [ev.finalize() for ev in all_events]
    existing = _existing_event_ids(conn, (ev.id for ev in finalized))
    by_source: dict[str, dict[str, int]] = {
        s: {"added": 0, "updated": 0, "errors": errors.get(s, 0)} for s in sources
    }
    for ev in finalized:
        bucket = by_source.setdefault(ev.source, {"added": 0, "updated": 0, "errors": 0})
        bucket["updated" if ev.id in existing else "added"] += 1

    added, updated = db.upsert_events(conn, finalized)
    n_links = db.upsert_links(conn, link_intents) if link_intents else 0
    if sessions:
        db.upsert_sessions(conn, sessions)

    # de-duplicate versions by id, then insert
    seen: set[str] = set()
    n_versions = 0
    for v in versions:
        if v["id"] in seen:
            continue
        seen.add(v["id"])
        path = v["doc_path"]
        db.upsert_doc_version(
            conn,
            vid=v["id"],
            doc_path=path,
            ts=v["ts"],
            content_hash=v["content_hash"],
            content=v["content"],
            diff=v.get("diff"),
        )
        n_versions += 1

    for source in sources:
        db.set_ingest_state(conn, source, cursor="full-rescan", note=f"{per_source.get(source, 0)} events")
        rid = db.start_run(conn, source)
        # PORT-NOTE: the old code wrote `added=per_source` (event count) and
        # always 0 updated/errors; the accurate per-source numbers now exist.
        bucket = by_source.get(source, {"added": 0, "updated": 0, "errors": errors.get(source, 0)})
        db.finish_run(
            conn,
            rid,
            added=bucket["added"],
            updated=bucket["updated"],
            errors=bucket["errors"],
        )

    mind_summary = mind.import_mind(conn)

    # Best-effort local embeddings for new/changed atoms. A small budget keeps a
    # first big backlog from stretching one ingest run; later runs continue.
    # Ollama being down is a no-op and never breaks ingest.
    embed_summary = None
    try:
        embed_summary = embeddings.embed_missing(conn, budget=24)
    except Exception as exc:  # noqa: BLE001 - embeddings must never break ingest
        embed_summary = {"error": str(exc)}

    # daily dream pass (consolidation) — at most once every ~20h
    row = conn.execute("SELECT updated_at FROM ingest_state WHERE source='consolidate'").fetchone()
    last = row[0] if row else 0
    dream = None
    if now_ms() - (last or 0) > 20 * 3600_000:
        dream = mind.consolidate(conn)
        # PORT-NOTE: db.set_ingest_state annotates cursor as str but the old
        # code stored an int here (the consolidate schedule reads updated_at, not
        # cursor, so behaviour is unchanged and parity with the old store holds).
        db.set_ingest_state(conn, "consolidate", cursor=now_ms(), note=str(dream))  # type: ignore[arg-type]

    return {
        "events_total": len(all_events),
        "added": added,
        "updated": updated,
        "links": n_links,
        "sessions": len(sessions),
        "doc_versions": n_versions,
        "per_source": per_source,
        "by_source": by_source,
        "mind": mind_summary,
        "embeddings": embed_summary,
        "dream": dream,
    }


def run_all(conn: sqlite3.Connection, *, sources: Iterable[str] | None = None) -> dict:
    """Run the adapters and report ``{source: {added, updated, errors}}``.

    The S6b contract entry point (the life loop's perceive phase calls this).
    ``sources=None`` runs the default set (opencode, git, docs).
    """
    result = run(conn, tuple(sources) if sources is not None else DEFAULT_SOURCES)
    if result.get("skipped"):
        return {"skipped": result["skipped"]}
    return result["by_source"]


def main() -> None:  # pragma: no cover - convenience
    conn = db.connect()
    db.init_db(conn)
    print(run(conn))


if __name__ == "__main__":  # pragma: no cover
    main()
