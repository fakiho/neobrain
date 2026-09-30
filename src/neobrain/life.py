"""neoBrain life loop (SPEC §6) — the in-process state machine.

Phases: **perceive** (health probes; failures become atoms) → **reflect**
(``mind.consolidate``) → **act** (one internal thought, stored as an atom) →
**rest** (nightly dream hook). Cadence lives in code + DB, *never* in OS timers.

State is derived from the ``events`` table (``source='life'``): every phase run
is recorded as a ``life: <phase>`` event, and "is this phase due?" is just
*now − last run ≥ configured interval*. A daemon restart therefore loses no
scheduling state — phases are simply due again once their interval has elapsed.

Quiet hours (``life_quiet_start`` … ``life_quiet_end``, local time) run only
``perceive``; ``act`` never runs then (SPEC open question #5 default: the agent
does not initiate contact — v0 stores a private observation and messages no
one). During rest, if the local hour equals ``life_dream_hour`` the loop invokes
``neobrain.dreams.run(conn, runtime)`` once per night, or emits a single
"dreams not wired yet (S6)" event per night when that module does not exist. On
``reflect_weekdays`` the same rest window also invokes ``neobrain.dreams.reflect``
once for the day (the old system ran reflect Sundays 04:00). ``perceive`` also
runs the ingest adapters via ``ingest.runner.run_all`` when a workspace or
OpenCode DB is configured.
"""
from __future__ import annotations

import importlib.util
import json
import socket
import sqlite3
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from . import config, db, mind
from .models import Event, event_id, now_ms

SOURCE = "life"
LANE = "notice"
CATEGORY = "life"
ACTOR = "agent:neobrain"
PHASES = ("perceive", "reflect", "act")

# The query used to pull the recent atoms that seed an `act` thought. It is
# intentionally broad: recall ranks whatever the mind currently holds as most
# relevant, and act is about forming an opinion on recent experience.
ACT_QUERY = "recent observations, open loops and decisions"
ACT_RECALL_LIMIT = 5

_DAY_MS = 86_400_000


# --- probes (module level so tests can patch them) --------------------------


def _probe_http(url: str, timeout: float, label: str) -> tuple[bool, str]:
    """Reachability probe: *any* HTTP answer means the endpoint is up.

    A 401/404 is a healthy server telling us we asked for the wrong thing, so
    only a transport failure or a 5xx counts as unreachable.
    """
    req = urllib.request.Request(url, method="GET", headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return True, f"{label} HTTP {resp.status}"
    except urllib.error.HTTPError as exc:
        if exc.code >= 500:
            return False, f"{label} HTTP {exc.code}"
        return True, f"{label} HTTP {exc.code}"
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        return False, f"{label} unreachable: {reason}"


def _check_llm(conn: Optional[sqlite3.Connection] = None, timeout: float = 5.0) -> tuple[bool, str]:
    return _probe_http(config.settings.llm_base_url.rstrip("/"), timeout, "LLM base_url")


def _check_embed(conn: Optional[sqlite3.Connection] = None, timeout: float = 5.0) -> tuple[bool, str]:
    return _probe_http(config.EMBED_URL, timeout, "embeddings endpoint")


def _check_data_dir(conn: Optional[sqlite3.Connection] = None) -> tuple[bool, str]:
    """The data dir must be creatable and writable (the store lives there)."""
    target = config.DATA_DIR
    try:
        target.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=target, prefix=".life_probe", delete=True) as fh:
            fh.write(b"ok")
            fh.flush()
        return True, f"data dir writable: {target}"
    except OSError as exc:
        return False, f"data dir not writable: {target} ({exc})"


def _check_db(conn: Optional[sqlite3.Connection] = None) -> tuple[bool, str]:
    if conn is None:
        return False, "no database connection"
    try:
        conn.execute("SELECT 1").fetchone()
        return True, "db responsive"
    except sqlite3.Error as exc:
        return False, f"db error: {exc}"


# default probe set, in report order
PROBES: tuple[tuple[str, Callable[..., tuple[bool, str]]], ...] = (
    ("llm", _check_llm),
    ("embeddings", _check_embed),
    ("data_dir", _check_data_dir),
    ("db", _check_db),
)


# --- helpers ----------------------------------------------------------------


def _default_clock() -> datetime:
    return datetime.now()


def _import_runtime():
    """Lazy import so merely importing life never needs a key or network."""
    from . import runtime

    return runtime


class LifeLoop:
    """The phase scheduler. Inject ``clock``/``runtime`` for tests.

    Everything is synchronous: ``tick()`` runs one pass and returns what ran;
    ``run_forever()`` sleeps ``life_tick_seconds`` between passes. No threads.
    """

    def __init__(
        self,
        db_path: Optional[Path | str] = None,
        clock: Optional[Callable[[], datetime]] = None,
        runtime: Any = None,
    ) -> None:
        self.db_path = Path(db_path) if db_path is not None else None
        self.clock = clock or _default_clock
        self._runtime = runtime
        self._conn: Optional[sqlite3.Connection] = None

    # --- connection ---------------------------------------------------------

    def connect(self) -> sqlite3.Connection:
        """Open (once) and initialise the store. Reused across ticks."""
        if self._conn is None:
            self._conn = db.connect(self.db_path)
            db.init_db(self._conn)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def runtime(self) -> Any:
        if self._runtime is None:
            self._runtime = _import_runtime()
        return self._runtime

    # --- events (state) -----------------------------------------------------

    def _emit(
        self,
        conn: sqlite3.Connection,
        phase: str,
        *,
        when_ms: int,
        next_due_ms: int,
        summary: str,
        severity: str = "info",
        extra: Optional[dict[str, Any]] = None,
    ) -> str:
        """Record a phase run as an event (the loop's only durable state)."""
        detail: dict[str, Any] = {
            "phase": phase,
            "next_due": next_due_ms,
            "summary": summary,
        }
        if extra:
            detail.update(extra)
        detail_json = json.dumps(detail, ensure_ascii=False)
        eid = event_id(SOURCE, f"{phase}:{when_ms}")
        # PORT-NOTE: S4/S5 review — go through db.upsert_events (the single
        # event writer) instead of a private INSERT + FTS pair, so the FTS
        # index can never drift from the events table.
        db.upsert_events(
            conn,
            [
                Event(
                    id=eid,
                    ts=when_ms,
                    source=SOURCE,
                    lane=LANE,
                    category=CATEGORY,
                    actor=ACTOR,
                    title=f"life: {phase}",
                    detail=detail_json,
                    severity=severity,
                    refs={"phase": phase},
                    raw=detail,
                )
            ],
        )
        return eid

    def _last_phase_ms(self, conn: sqlite3.Connection, phase: str) -> Optional[int]:
        row = conn.execute(
            "SELECT MAX(ts) FROM events WHERE source=? AND category=? AND title=?",
            (SOURCE, CATEGORY, f"life: {phase}"),
        ).fetchone()
        return int(row[0]) if row and row[0] is not None else None

    def _last_event_detail(self, conn: sqlite3.Connection, phase: str) -> dict[str, Any]:
        row = conn.execute(
            "SELECT raw, detail FROM events WHERE source=? AND title=? ORDER BY ts DESC LIMIT 1",
            (SOURCE, f"life: {phase}"),
        ).fetchone()
        if row is None:
            return {}
        for value in row:
            if not value:
                continue
            try:
                parsed = json.loads(value)
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(parsed, dict):
                return parsed
        return {}

    # --- cadence ------------------------------------------------------------

    def _interval_minutes(self, phase: str) -> int:
        s = config.settings
        return {
            "perceive": s.life_perceive_interval_minutes,
            "reflect": s.life_reflect_interval_minutes,
            "act": s.life_act_interval_minutes,
        }[phase]

    def _due(self, conn: sqlite3.Connection, phase: str, when_ms: int) -> bool:
        last = self._last_phase_ms(conn, phase)
        if last is None:
            return True
        return when_ms - last >= self._interval_minutes(phase) * 60_000

    def _next_due(self, when_ms: int, phase: str) -> int:
        return when_ms + self._interval_minutes(phase) * 60_000

    def is_quiet(self, now: Optional[datetime] = None) -> bool:
        """True inside quiet hours (start inclusive, end exclusive, wrap-aware)."""
        now = now or self.clock()
        start, end = config.settings.life_quiet_start, config.settings.life_quiet_end
        hour = now.hour
        if start == end:
            return False
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end

    # --- phases -------------------------------------------------------------

    def _perceive(self, conn: sqlite3.Connection, when: int) -> dict[str, Any]:
        results: dict[str, dict[str, Any]] = {}
        failures: list[tuple[str, str]] = []
        for name, probe in PROBES:
            try:
                ok, note = probe(conn)
            except Exception as exc:  # noqa: BLE001 - a probe must never break the loop
                ok, note = False, f"{type(exc).__name__}: {exc}"
            results[name] = {"ok": ok, "note": note}
            if not ok:
                failures.append((name, note))

        # Throttle: remember a failure only when it is *new* relative to the
        # previous perceive event, i.e. once per failure-streak. Derived from
        # the event log, so a restart mid-streak does not re-remember.
        previous = self._last_event_detail(conn, "perceive")
        already = set(previous.get("failures") or [])
        remembered: list[str] = []
        for name, note in failures:
            if name in already:
                continue
            try:
                atom = mind.remember(
                    conn,
                    f"Health probe failed: {name} — {note}",
                    atype="observation",
                    hubs=["neobrain", "life"],
                    source="life:perceive",
                )
                remembered.append(str(atom.get("id")))
            except Exception as exc:  # noqa: BLE001 - memory of a failure is best-effort
                results[name]["remember_error"] = str(exc)

        # PORT-NOTE: S6b — ingest runs inside perceive, after the health probes
        # (SPEC §6: perceive = ingest sources + health checks). Guarded: without
        # a configured workspace or OpenCode DB there is nothing to read, and a
        # broken run becomes a warning on the event, never a crash.
        ingest_summary = None
        ingest_error = None
        if config.WORKSPACE is not None or config.OPENCODE_DB is not None:
            try:
                from .ingest import runner as ingest_runner

                ingest_summary = ingest_runner.run_all(conn)
            except Exception as exc:  # noqa: BLE001 - ingest must never stop the loop
                ingest_error = f"{type(exc).__name__}: {exc}"

        failed_names = [name for name, _ in failures]
        if not failures:
            summary = "all probes ok"
        else:
            summary = "failed: " + ", ".join(failed_names)
        if ingest_error:
            summary = f"{summary}; ingest failed: {ingest_error}"
        self._emit(
            conn,
            "perceive",
            when_ms=when,
            next_due_ms=self._next_due(when, "perceive"),
            summary=summary,
            severity="warning" if (failures or ingest_error) else "info",
            extra={
                "checks": results,
                "failures": failed_names,
                "remembered": remembered,
                "ingest": ingest_summary,
                "ingest_error": ingest_error,
            },
        )
        return {
            "failures": failed_names,
            "remembered": remembered,
            "checks": results,
            "ingest": ingest_summary,
            "ingest_error": ingest_error,
        }

    def _reflect(self, conn: sqlite3.Connection, when: int) -> dict[str, Any]:
        result = mind.consolidate(conn)
        summary = (
            f"consolidate: {result.get('merged_hubs', 0)} hub alias(es), "
            f"{result.get('promoted', 0)} pattern(s), "
            f"{result.get('notes_promoted', 0)} day-note section(s), "
            f"{result.get('superseded', 0)} supersession(s)"
        )
        self._emit(
            conn,
            "reflect",
            when_ms=when,
            next_due_ms=self._next_due(when, "reflect"),
            summary=summary,
            extra={"result": result},
        )
        return result

    def _act(self, conn: sqlite3.Connection, when: int) -> dict[str, Any]:
        """One internal thought. The agent messages nobody (SPEC open q. #5)."""
        recalled = mind.recall(conn, ACT_QUERY, limit=ACT_RECALL_LIMIT)
        atoms = recalled.get("atoms") or []
        system = _act_system_prompt(atoms)
        thought = ""
        error: Optional[str] = None
        try:
            thought = (
                self.runtime().chat(
                    [
                        {"role": "system", "content": system},
                        {
                            "role": "user",
                            "content": (
                                "Write one short first-person observation or opinion, "
                                "under 60 words. No preamble, no lists."
                            ),
                        },
                    ],
                    model="strong",
                    temperature=0.8,
                    max_tokens=200,
                )
                or ""
            ).strip()
        except Exception as exc:  # noqa: BLE001 - a failed thought is not a crash
            error = f"{type(exc).__name__}: {exc}"

        atom_id: Optional[str] = None
        if thought:
            links = {}
            source_ids = [a["id"] for a in atoms if a.get("id")]
            if source_ids:
                links["derived-from"] = source_ids
            atom = mind.remember(
                conn,
                thought,
                atype="observation",
                hubs=["neobrain", "life"],
                source="life:act",
                links=links,
            )
            atom_id = str(atom.get("id"))

        summary = thought[:200] if thought else (f"no thought produced ({error})" if error else "empty thought")
        self._emit(
            conn,
            "act",
            when_ms=when,
            next_due_ms=self._next_due(when, "act"),
            summary=summary,
            severity="warning" if error else "info",
            extra={
                "atom": atom_id,
                "recalled": [a.get("id") for a in atoms],
                "model": "strong",
                "error": error,
            },
        )
        return {"atom": atom_id, "thought": thought, "error": error}

    def _dream_ran_tonight(self, conn: sqlite3.Connection, now: datetime) -> bool:
        row = conn.execute(
            "SELECT MAX(ts) FROM events WHERE source=? AND title=?",
            (SOURCE, "life: dream"),
        ).fetchone()
        if not row or row[0] is None:
            return False
        return datetime.fromtimestamp(int(row[0]) / 1000).date() == now.date()

    def _reflect_ran_today(self, conn: sqlite3.Connection, now: datetime) -> bool:
        row = conn.execute(
            "SELECT MAX(ts) FROM events WHERE source=? AND title=?",
            (SOURCE, "life: soul-reflect"),
        ).fetchone()
        if not row or row[0] is None:
            return False
        return datetime.fromtimestamp(int(row[0]) / 1000).date() == now.date()

    def _rest(self, conn: sqlite3.Connection, now: datetime, when: int) -> list[str]:
        if now.hour != config.settings.life_dream_hour:
            return []
        ran: list[str] = []

        # --- nightly dream phases ------------------------------------------
        if not self._dream_ran_tonight(conn, now):
            if importlib.util.find_spec("neobrain.dreams") is None:
                self._emit(
                    conn,
                    "dream",
                    when_ms=when,
                    next_due_ms=when + _DAY_MS,
                    summary="dreams not wired yet (S6)",
                    severity="notice",
                )
                ran.append("dream")
            else:
                from . import dreams  # type: ignore[attr-defined]

                summary = "dream pass complete"
                severity = "info"
                try:
                    result = dreams.run(conn, self.runtime(), now=now)
                    if isinstance(result, dict):
                        summary = f"dream pass: {result.get('status', 'ok')} ({result.get('date', '')})"
                    else:
                        summary = f"dream pass: {result}"
                except Exception as exc:  # noqa: BLE001 - a broken night must not stop the loop
                    summary = f"dream pass failed: {type(exc).__name__}: {exc}"
                    severity = "error"
                # PORT-NOTE: S6b — dreams.run records its own "life: dream"
                # marker + summary (single-writer assumption replaces dream.sh's
                # flock). Only emit here when it did not, so one night gets one
                # marker and the once-per-night guard holds.
                if not self._dream_ran_tonight(conn, now):
                    self._emit(
                        conn,
                        "dream",
                        when_ms=when,
                        next_due_ms=when + _DAY_MS,
                        summary=summary,
                        severity=severity,
                    )
                ran.append("dream")

        # --- soul reflection (reflect.sh ran Sundays 04:00) ----------------
        # PORT-NOTE: S6b — runs on the settings.reflect_weekdays days at the
        # dream hour, once per day; the guard is the "life: soul-reflect" event.
        if (
            now.weekday() in config.reflect_days()
            and not self._reflect_ran_today(conn, now)
            and importlib.util.find_spec("neobrain.dreams") is not None
        ):
            from . import dreams  # type: ignore[attr-defined]

            try:
                dreams.reflect(conn, self.runtime(), now=now)
            except Exception as exc:  # noqa: BLE001 - a broken ritual must not stop the loop
                self._emit(
                    conn,
                    "soul-reflect",
                    when_ms=when,
                    next_due_ms=when + _DAY_MS,
                    summary=f"soul reflect failed: {type(exc).__name__}: {exc}",
                    severity="error",
                )
            if not self._reflect_ran_today(conn, now):
                self._emit(
                    conn,
                    "soul-reflect",
                    when_ms=when,
                    next_due_ms=when + _DAY_MS,
                    summary="soul reflection complete",
                    severity="info",
                )
            ran.append("soul-reflect")
        return ran

    # --- the tick -----------------------------------------------------------

    def tick(self, now: Optional[datetime] = None) -> dict[str, Any]:
        """Run every phase that is due. Returns {"ran": [...], "quiet": bool}."""
        now = now or self.clock()
        when = int(now.timestamp() * 1000)
        conn = self.connect()
        ran: list[str] = []
        quiet = self.is_quiet(now)

        if self._due(conn, "perceive", when):
            self._perceive(conn, when)
            ran.append("perceive")

        if quiet:
            ran.extend(self._rest(conn, now, when))
        else:
            if self._due(conn, "reflect", when):
                self._reflect(conn, when)
                ran.append("reflect")
            if self._due(conn, "act", when):
                self._act(conn, when)
                ran.append("act")

        return {"ran": ran, "quiet": quiet, "ts": when}

    def run_forever(self, max_ticks: Optional[int] = None) -> dict[str, Any]:
        """Blocking daemon loop: tick, then sleep ``life_tick_seconds``."""
        interval = max(1, config.settings.life_tick_seconds)
        ticks = 0
        try:
            while max_ticks is None or ticks < max_ticks:
                self.tick()
                ticks += 1
                if max_ticks is not None and ticks >= max_ticks:
                    break
                time.sleep(interval)
        except KeyboardInterrupt:
            return {"ticks": ticks, "stopped": "keyboard"}
        return {"ticks": ticks}


def _act_system_prompt(atoms: list[dict[str, Any]]) -> str:
    lines = [
        "You are neoBrain, an autonomous agent with your own durable memory.",
        "You are writing a private journal entry; nobody is reading it in real time.",
        "Your five most relevant recent memories:",
    ]
    for atom in atoms:
        text = (atom.get("text") or atom.get("label") or "").strip().replace("\n", " ")
        lines.append(f"- [{atom.get('type')}] {text[:280]}")
    if not atoms:
        lines.append("- (no memories yet)")
    lines.append(
        "Form one short first-person observation or opinion grounded in these memories."
    )
    return "\n".join(lines)
