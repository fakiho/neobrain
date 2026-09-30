"""neoBrain read/ingest API (FastAPI) + daemon wiring.

Run:  .venv/bin/uvicorn neobrain.api:app --host 0.0.0.0 --port 9192
      neobrain start        # the CLI wrapper (config.settings.bind)
"""
from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from collections import deque
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config, db, mind
from .models import iso, new, now_ms, to_ms

# PORT-NOTE: the old in-API ingest scheduler (INGEST_INTERVAL / _ingest_loop) is
# GONE — SPEC §6 puts cadence in the life loop (life.py), not in OS/API timers.
# "The daemon starts the life loop" is the only background work here; ingestion
# is a life-loop phase (and is still reachable one-shot via POST /api/ingest/run
# and the `neobrain ingest` CLI).


def _life_enabled() -> bool:
    """NEOBRAIN_LIFE_ENABLED: any of 0/false/no/off disables the loop."""
    return str(config.settings.life_enabled).strip().lower() not in ("0", "false", "no", "off")


def _announce_empty_mind(conn) -> None:
    """SPEC §14 S6 hardening: a brand-new empty mind must never boot silently.

    Real-data validation found a wrong ``NEOBRAIN_DATA_DIR`` silently
    auto-created an empty DB, and the dashboard looked "empty" with no hint of
    why. The boot itself is now a durable, dashboard-visible event (source
    ``neobrain``). It is emitted only while the event log is empty, so a healthy
    restart never re-announces.
    """
    try:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
    except Exception as exc:  # noqa: BLE001 - announcement must never block boot
        print(f"[startup] empty-mind check failed: {exc}", flush=True)
        return
    if total:
        return
    ev = new(
        ts=now_ms(),
        source="neobrain",
        lane="notice",
        category="boot",
        actor="daemon:neobrain",
        title="neoBrain started with an empty mind — no events and no memories yet",
        source_ref="boot",
        severity="notice",
        detail=json.dumps(
            {
                "data_dir": str(config.DATA_DIR),
                "db_path": str(config.DB_PATH),
                "hint": "if this path is wrong, stop the daemon and set NEOBRAIN_DATA_DIR",
            },
            ensure_ascii=False,
        ),
        refs={"data_dir": str(config.DATA_DIR)},
    )
    db.upsert_events(conn, [ev])
    print(f"[startup] empty mind announced (data dir {config.DATA_DIR})", flush=True)


def _life_thread(loop: Any, stop: threading.Event) -> None:
    """Tick the life loop until ``stop`` is set (the Run-forever body, stoppable)."""
    interval = max(1, config.settings.life_tick_seconds)
    try:
        while not stop.is_set():
            try:
                loop.tick()
            except Exception as exc:  # noqa: BLE001 - a bad tick must not kill the daemon
                print(f"[life] tick error: {exc}", flush=True)
            stop.wait(interval)
    finally:
        # PORT-NOTE: LifeLoop's sqlite handle is thread-affine (created inside
        # this thread), so it must be closed here — closing it from the lifespan
        # thread raises `SQLite objects created in a thread can only be used in
        # that same thread` and fails application shutdown.
        try:
            loop.close()
        except Exception as exc:  # noqa: BLE001 - shutdown must still complete
            print(f"[life] close error: {exc}", flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # ensure the store exists before serving
    try:
        conn = db.connect()
        db.init_db(conn)
        _announce_empty_mind(conn)
        conn.close()
    except Exception as exc:
        print(f"[startup] store init failed: {exc}", flush=True)

    # PORT-NOTE: the daemon (not the CLI process, not a systemd unit) owns the
    # in-process life loop (SPEC §6). It runs in a daemon thread; shutdown sets
    # the event and joins so the loop's SQLite handle is closed cleanly.
    stop_event = threading.Event()
    thread: Optional[threading.Thread] = None
    loop: Any = None
    if _life_enabled():
        from .life import LifeLoop

        loop = LifeLoop()
        thread = threading.Thread(
            target=_life_thread, args=(loop, stop_event), name="neobrain-life", daemon=True
        )
        thread.start()
        print(f"[startup] life loop started (tick {config.settings.life_tick_seconds}s)", flush=True)
    else:
        print("[startup] life loop disabled (NEOBRAIN_LIFE_ENABLED=0)", flush=True)

    try:
        yield
    finally:
        stop_event.set()
        if thread is not None:
            # The thread closes the loop's own sqlite handle (see _life_thread).
            thread.join(timeout=10)


app = FastAPI(
    # PORT-NOTE: product rename Timeline -> neoBrain (S1 contract).
    title="neoBrain",
    version="0.1.0",
    docs_url="/api/schema",
    redoc_url=None,
    openapi_url="/api/openapi.json",
    lifespan=lifespan,
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# Debug page (SPEC §5 visibility): rolling trace of every /api call — who called
# what, when, with what session and latency. In-memory only (lost on restart,
# which is fine: it answers "is the plugin firing right now", not forensics).
_TRACE: deque = deque(maxlen=500)


@app.middleware("http")
async def _trace_requests(request: Request, call_next):
    # Skip non-API and the debug page's own polling: it refreshes every 5s and
    # would drown the plugin lanes the trace exists to watch.
    if not request.url.path.startswith("/api/") or request.url.path.startswith("/api/debug"):
        return await call_next(request)
    t0 = time.perf_counter()
    response = await call_next(request)
    _TRACE.append(
        {
            "ts": int(time.time() * 1000),
            "method": request.method,
            "path": request.url.path,
            "query": request.url.query,
            "status": response.status_code,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "session": request.query_params.get("session") or "",
        }
    )
    return response


def _conn():
    return db.connect(readonly=True)


def _serialize(ev: dict) -> dict:
    ev = dict(ev)
    ev["ts_iso"] = iso(ev.get("ts"))
    if ev.get("ts_end"):
        ev["ts_end_iso"] = iso(ev["ts_end"])
    return ev


def _parse_time(value: Optional[str]) -> Optional[int]:
    if not value:
        return None
    ms = to_ms(value)
    if ms is not None:
        return ms
    try:  # date-only -> local start/end of that day handled by caller
        return int(datetime.strptime(value, "%Y-%m-%d").timestamp() * 1000)
    except ValueError:
        return None


def _day_bounds(date_str: str) -> tuple[int, int]:
    dt = datetime.strptime(date_str, "%Y-%m-%d").astimezone()
    start = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end = dt.replace(hour=23, minute=59, second=59, microsecond=999000)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)


@app.get("/api/health")
def health() -> dict:
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM events").fetchone()[0]
        states = [
            dict(r) for r in conn.execute("SELECT source, cursor, updated_at FROM ingest_state")
        ]
    except Exception as exc:  # store not initialised yet
        raise HTTPException(503, f"store not ready: {exc}")
    finally:
        conn.close()
    return {"status": "ok", "events": total, "sources": states}


@app.get("/api/stats")
def stats() -> dict:
    conn = _conn()
    try:
        s = db.stats(conn)
    finally:
        conn.close()
    if s.get("range"):
        s["range"]["min_iso"] = iso(s["range"].get("min"))
        s["range"]["max_iso"] = iso(s["range"].get("max"))
    return s


@app.get("/api/events")
def list_events(
    start: Optional[str] = None,
    end: Optional[str] = None,
    day: Optional[str] = None,
    lane: Optional[str] = None,
    source: Optional[str] = None,
    category: Optional[str] = None,
    project: Optional[str] = None,
    session: Optional[str] = None,
    q: Optional[str] = None,
    limit: int = Query(200, le=20000),
    offset: int = 0,
) -> dict:
    s, e = _parse_time(start), _parse_time(end)
    if day:
        s, e = _day_bounds(day)
    conn = _conn()
    try:
        rows = db.events(
            conn,
            start=s,
            end=e,
            lane=lane,
            source=source,
            category=category,
            project_id=project,
            session_id=session,
            q=q,
            limit=limit,
            offset=offset,
        )
    finally:
        conn.close()
    return {"count": len(rows), "offset": offset, "events": [_serialize(r) for r in rows]}


@app.get("/api/events/{event_id}")
def get_event(event_id: str) -> dict:
    conn = _conn()
    try:
        ev = db.get_event(conn, event_id)
    finally:
        conn.close()
    if not ev:
        raise HTTPException(404, "event not found")
    return _serialize(ev)


@app.get("/api/day/{date}")
def day(date: str) -> dict:
    s, e = _day_bounds(date)
    conn = _conn()
    try:
        rows = db.events(conn, start=s, end=e, limit=2000)
    finally:
        conn.close()
    by_lane: dict[str, list[dict]] = {}
    for r in rows:
        by_lane.setdefault(r["lane"], []).append(_serialize(r))
    return {"date": date, "count": len(rows), "lanes": by_lane}


@app.get("/api/sessions")
def sessions() -> dict:
    conn = _conn()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM sessions ORDER BY ts_created DESC"
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["ts_created_iso"] = iso(r.get("ts_created"))
    return {"count": len(rows), "sessions": rows}


@app.get("/api/sessions/{session_id}")
def session_timeline(session_id: str, limit: int = Query(2000, le=5000)) -> dict:
    conn = _conn()
    try:
        rows = db.events(conn, session_id=session_id, limit=limit)
    finally:
        conn.close()
    return {"session_id": session_id, "count": len(rows), "events": [_serialize(r) for r in rows]}


@app.get("/api/search")
def search(q: str, limit: int = Query(100, le=1000)) -> dict:
    conn = _conn()
    try:
        rows = db.events(conn, q=q, limit=limit)
    finally:
        conn.close()
    return {"query": q, "count": len(rows), "events": [_serialize(r) for r in rows]}


@app.get("/api/chain/{event_id}")
def chain(event_id: str) -> dict:
    conn = _conn()
    try:
        ev = db.get_event(conn, event_id)
        if not ev:
            raise HTTPException(404, "event not found")
        ch = db.chain(conn, event_id)
        for direction in ("in", "out"):
            for link in ch[direction]:
                other = link["from_id"] if direction == "in" else link["to_id"]
                oe = db.get_event(conn, other)
                link["event"] = _serialize(oe) if oe else None
    finally:
        conn.close()
    return {"event": _serialize(ev), **ch}


@app.get("/api/docs")
def docs_index() -> dict:
    conn = _conn()
    try:
        rows = [
            dict(r)
            for r in conn.execute(
                """SELECT doc_path, COUNT(*) AS versions, MAX(ts) AS last_ts
                   FROM doc_versions GROUP BY doc_path ORDER BY last_ts DESC"""
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["last_iso"] = iso(r.get("last_ts"))
    return {"count": len(rows), "docs": rows}


@app.get("/api/docs/history")
def docs_history(path: str) -> dict:
    conn = _conn()
    try:
        rows = [
            {k: v for k, v in dict(r).items() if k != "content"}
            for r in conn.execute(
                "SELECT * FROM doc_versions WHERE doc_path = ? ORDER BY ts DESC", (path,)
            )
        ]
    finally:
        conn.close()
    for r in rows:
        r["ts_iso"] = iso(r.get("ts"))
    return {"path": path, "count": len(rows), "versions": rows}


@app.post("/api/ingest/run")
def ingest_run(only: Optional[str] = None) -> dict:
    # PORT-NOTE: one-shot ingest stays available; the periodic scheduler that
    # used to call this is gone (SPEC §6, life loop owns cadence).
    from .ingest import runner

    conn = db.connect()
    try:
        db.init_db(conn)
        sources = tuple(only.split(",")) if only else ("opencode", "git", "docs")
        return runner.run(conn, sources)
    finally:
        conn.close()


# --- mind model (M1) ----------------------------------------------------
@app.get("/api/mind/stats")
def mind_stats() -> dict:
    conn = _conn()
    try:
        return mind.stats(conn)
    finally:
        conn.close()


@app.get("/api/mind/graph")
def mind_graph() -> dict:
    conn = _conn()
    try:
        return mind.graph(conn)
    finally:
        conn.close()


@app.get("/api/mind/atom/{atom_id}")
def mind_atom(atom_id: str) -> dict:
    conn = _conn()
    try:
        a = mind.atom_detail(conn, atom_id)
    finally:
        conn.close()
    if not a:
        raise HTTPException(404, "atom not found")
    a["created_iso"] = iso(a.get("created"))
    return a


@app.get("/api/mind/activity")
def mind_activity(limit: int = Query(60, le=200)) -> dict:
    conn = _conn()
    try:
        return {"ops": mind.activity(conn, limit)}
    finally:
        conn.close()


@app.get("/api/mind/memories")
def mind_memories(
    q: Optional[str] = None,
    type: Optional[list[str]] = Query(None),
    hub: Optional[list[str]] = Query(None),
    limit: int = Query(500, le=5000),
    offset: int = 0,
) -> dict:
    """The Memory feed: every atom in the mind, newest first, filterable."""
    conn = _conn()
    try:
        return mind.memories(conn, q=q, types=type, hubs=hub, limit=limit, offset=offset)
    finally:
        conn.close()


@app.post("/api/mind/import")
def mind_import() -> dict:
    conn = db.connect()
    try:
        db.init_db(conn)
        return mind.import_mind(conn)
    finally:
        conn.close()


class RememberBody(BaseModel):
    text: str
    type: str = "observation"
    hubs: Optional[list[str]] = None
    links: Optional[dict[str, list[str]]] = None
    source: Optional[str] = None
    session: Optional[str] = None
    label: Optional[str] = None
    dedupe: bool = False


@app.post("/api/mind/remember")
def mind_remember(body: RememberBody) -> dict:
    conn = db.connect()
    try:
        db.init_db(conn)
        return mind.remember(
            conn, body.text, atype=body.type, hubs=body.hubs, links=body.links,
            source=body.source, session_id=body.session, label=body.label,
            dedupe=body.dedupe,
        )
    finally:
        conn.close()


class FeedbackBody(BaseModel):
    atom_id: str
    signal: str
    source: Optional[str] = None
    session: Optional[str] = None


@app.post("/api/mind/feedback")
def mind_feedback(body: FeedbackBody) -> dict:
    """Record a memory quality signal (used|useful|noise); log best-effort."""
    conn = db.connect()
    try:
        db.init_db(conn)
        try:
            return mind.feedback(
                conn, body.atom_id, body.signal,
                source=body.source, session_id=body.session,
            )
        except ValueError as exc:
            raise HTTPException(400, str(exc))
    finally:
        conn.close()


@app.get("/api/mind/recall")
def mind_recall(q: str, limit: int = Query(8, le=50), session: Optional[str] = None) -> dict:
    conn = db.connect()
    try:
        db.init_db(conn)
        return mind.recall(conn, q, limit=limit, session_id=session)
    finally:
        conn.close()


@app.get("/api/mind/wakeup")
def mind_wakeup(session: Optional[str] = None) -> dict:
    conn = db.connect()
    try:
        db.init_db(conn)
        return mind.wake_up(conn, session)
    finally:
        conn.close()


@app.post("/api/mind/consolidate")
def mind_consolidate(session: Optional[str] = None) -> dict:
    conn = db.connect()
    try:
        db.init_db(conn)
        return mind.consolidate(conn, session)
    finally:
        conn.close()


@app.get("/api/mind/dreams")
def mind_dreams(limit: int = Query(30, le=200)) -> dict:
    conn = _conn()
    try:
        return mind.dreams(conn, limit)
    finally:
        conn.close()


@app.get("/api/mind/reader")
def mind_reader(group: str) -> dict:
    conn = _conn()
    try:
        return mind.reader(conn, group)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    finally:
        conn.close()


# --- static UI ----------------------------------------------------------
# ---------------------------------------------------------------------------
# Debug page (SPEC §5 visibility): one aggregate view of what the daemon and
# the plugin actually did. Read-only, cheap, no auth change — same as the rest
# of the read API.
# ---------------------------------------------------------------------------


@app.get("/api/debug/requests")
def debug_requests(limit: int = Query(200, le=500)) -> dict:
    return {"requests": list(_TRACE)[-limit:], "buffer": len(_TRACE), "maxlen": _TRACE.maxlen}


@app.get("/api/debug/overview")
def debug_overview() -> dict:
    conn = _conn()
    conn.row_factory = sqlite3.Row
    now_ms_ = now_ms()
    day_start = int(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000)
    one = lambda sql: conn.execute(sql).fetchone()[0]  # noqa: E731

    counts = {
        "atoms": one("SELECT COUNT(*) FROM m_atoms"),
        "hubs": one("SELECT COUNT(*) FROM m_hubs"),
        "edges": one("SELECT COUNT(*) FROM m_edges"),
        "events": one("SELECT COUNT(*) FROM events"),
        "ops_today": one(f"SELECT COUNT(*) FROM m_ops WHERE ts >= {day_start}"),
        "feedback_24h": one(f"SELECT COUNT(*) FROM m_feedback WHERE ts >= {now_ms_ - 86_400_000}"),
    }
    rank_states = [dict(r) for r in conn.execute("SELECT state, COUNT(*) AS n FROM m_rank GROUP BY state ORDER BY n DESC")]
    atom_types = [dict(r) for r in conn.execute("SELECT type, COUNT(*) AS n FROM m_atoms GROUP BY type ORDER BY n DESC")]

    # Mind op log: the closest thing to a brain-side trace of the plugin lanes.
    ops_today = [dict(r) for r in conn.execute(f"SELECT op, COUNT(*) AS n FROM m_ops WHERE ts >= {day_start} GROUP BY op ORDER BY n DESC")]
    recent_ops = [
        dict(r) for r in conn.execute("SELECT ts, op, atom_id, label, query, session_id FROM m_ops ORDER BY id DESC LIMIT 40")
    ]
    feedback = [
        dict(r) for r in conn.execute("SELECT ts, atom_id, signal, source, session_id FROM m_feedback ORDER BY id DESC LIMIT 20")
    ]
    ingest = [
        dict(r)
        for r in conn.execute(
            "SELECT started_at, finished_at, source, added, updated, errors, note FROM ingest_runs ORDER BY id DESC LIMIT 12"
        )
    ]

    # Life loop: phases are due-based; state is fully derivable from the events
    # the loop already emits (actor agent:neobrain, category life, title "life: <phase>").
    s = config.settings
    hour = datetime.now().hour
    quiet_now = hour >= int(s.life_quiet_start) or hour < int(s.life_quiet_end)
    phases = []
    for name, interval in (
        ("perceive", s.life_perceive_interval_minutes),
        ("reflect", s.life_reflect_interval_minutes),
        ("act", s.life_act_interval_minutes),
    ):
        row = conn.execute(
            "SELECT ts, detail FROM events WHERE actor='agent:neobrain' AND category='life' AND title=? ORDER BY ts DESC LIMIT 1",
            (f"life: {name}",),
        ).fetchone()
        last = None
        if row:
            detail = {}
            try:
                detail = json.loads(row["detail"] or "{}")
            except Exception:
                pass
            last = {
                "ts": row["ts"],
                "summary": detail.get("summary") or "",
                "next_due": detail.get("next_due"),
            }
        phases.append({"phase": name, "every_minutes": interval, "last": last})
    last_dream = conn.execute("SELECT ts, label FROM m_ops WHERE op='dream' ORDER BY id DESC LIMIT 1").fetchone()

    config_view = {
        "life_enabled": _life_enabled(),
        "quiet_hours": f"{s.life_quiet_start:02d}:00–{s.life_quiet_end:02d}:00",
        "quiet_now": quiet_now,
        "dream_hour": f"{s.life_dream_hour:02d}:00",
        "reflect_weekday": getattr(s, "reflect_weekday", "?"),
        "tick_seconds": s.life_tick_seconds,
        "rank_half_life_days": s.rank_half_life_days,
        "llm_model_cheap": s.llm_model_cheap,
        "llm_model_strong": s.llm_model_strong,
        "bind": s.bind,
        "data_dir": str(config.DATA_DIR),
    }
    conn.close()
    return {
        "now": now_ms_,
        "counts": counts,
        "rank_states": rank_states,
        "atom_types": atom_types,
        "ops_today": ops_today,
        "recent_ops": recent_ops,
        "feedback": feedback,
        "ingest": ingest,
        "phases": phases,
        "quiet_now": quiet_now,
        "last_dream": dict(last_dream) if last_dream else None,
        "config": config_view,
    }


# Built SPA lives in web/dist; mounted last so /api/* always wins.
#
# PORT-NOTE: the old app/ layout had the package one level under the repo root
# (parent.parent), but this package sits at src/neobrain, so the repo root is
# parents[2]. NEOBRAIN_WEB_DIST overrides the location without a config.py key
# (config.py is frozen for this sprint) — resolved right here.
_web_dist = os.environ.get("NEOBRAIN_WEB_DIST")
DIST = (
    Path(_web_dist).expanduser().resolve()
    if _web_dist
    else Path(__file__).resolve().parents[2] / "web" / "dist"
)
if DIST.is_dir():
    app.mount("/", StaticFiles(directory=str(DIST), html=True), name="ui")
