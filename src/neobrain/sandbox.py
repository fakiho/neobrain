"""Counterfactual Dream-Sandbox (`neobrain.sandbox`).

Empirical nocturnal R&D engine: selects an open question or architectural
assumption from the mind graph, synthesizes a small falsifiable Python
experiment, validates it via a deterministic static AST safety gate, executes it
in an isolated (`python3 -I`) temporary directory with a scrubbed environment
and strict timeout, writes `<dream_root>/sandbox/<date>.{json,md}`, and promotes
confirmed or refuted findings back into the mind as `lesson` atoms linked
`derived-from` the seed memory.
"""
from __future__ import annotations

import ast
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from . import config, db, mind
from .models import Event, event_id, iso

SOURCE = "life"
LANE = "notice"
CATEGORY = "life"
ACTOR = "agent:neobrain"
EVENT_TITLE = "life: dream sandbox"
_DAY_MS = 86_400_000

FORBIDDEN_IMPORTS: frozenset[str] = frozenset(
    {
        "socket",
        "urllib",
        "http",
        "requests",
        "httpx",
        "aiohttp",
        "ftplib",
        "smtplib",
        "telnetlib",
        "xmlrpc",
        "subprocess",
        "shutil",
        "ctypes",
        "multiprocessing",
        "signal",
        "pty",
        "fcntl",
        "resource",
        "webbrowser",
        "ensuredir",
        "pip",
        "venv",
        "importlib",
        "builtins",
    }
)

FORBIDDEN_BUILTINS: frozenset[str] = frozenset(
    {
        "eval",
        "exec",
        "compile",
        "__import__",
        "input",
        "breakpoint",
        "getattr",
        "setattr",
        "delattr",
        "globals",
        "locals",
        "vars",
        "open",
    }
)

FORBIDDEN_OS_ATTRS: frozenset[str] = frozenset(
    {
        "system",
        "popen",
        "fork",
        "forkpty",
        "kill",
        "killpg",
        "remove",
        "unlink",
        "rmdir",
        "removedirs",
        "rename",
        "renames",
        "replace",
        "chmod",
        "fchmod",
        "lchmod",
        "chown",
        "fchown",
        "lchown",
        "chroot",
        "symlink",
        "link",
    }
)

FORBIDDEN_OS_PREFIXES: tuple[str, ...] = ("spawn", "exec")


def _dream_date(now: Optional[datetime] = None) -> str:
    """Resolve the dream date: before noon belongs to the previous day's date."""
    dt = now or datetime.now()
    day = dt.date() - timedelta(days=1) if dt.hour < 12 else dt.date()
    return day.isoformat()


def _dream_root() -> Path:
    """Directory where dream notes and sandbox artifacts live."""
    base = Path(config.WORKSPACE) if config.WORKSPACE is not None else Path(config.DATA_DIR)
    return base / "memory" / "dreaming"


def _day_note_path(date: str) -> Path:
    base = Path(config.WORKSPACE) if config.WORKSPACE is not None else Path(config.DATA_DIR)
    return base / "memory" / f"{date}.md"


def _is_forbidden_os_attr(attr: str) -> bool:
    return attr in FORBIDDEN_OS_ATTRS or attr.startswith(FORBIDDEN_OS_PREFIXES)


def validate_code(code: str) -> tuple[bool, Optional[str]]:
    """Deterministically validate Python experiment source via static AST inspection."""
    if not isinstance(code, str) or not code.strip():
        return False, "empty code"
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return False, f"syntax error: {exc}"

    if not tree.body:
        return False, "empty code"

    os_aliases: set[str] = {"os", "posix", "nt", "pathlib"}

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = alias.name.split(".")[0]
                if top in FORBIDDEN_IMPORTS:
                    return False, f"forbidden import: {top} ({alias.name})"
                if top in ("os", "posix", "nt", "pathlib"):
                    os_aliases.add(alias.asname or top)

        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            top = mod.split(".")[0] if mod else ""
            if top in FORBIDDEN_IMPORTS:
                return False, f"forbidden import: {top} (from {mod})"
            for alias in node.names:
                sub_top = alias.name.split(".")[0]
                if sub_top in FORBIDDEN_IMPORTS:
                    return False, f"forbidden import: {sub_top}"
                if top in ("os", "posix", "nt", "pathlib") and _is_forbidden_os_attr(alias.name):
                    return False, f"forbidden os mutation import: {top}.{alias.name}"

    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            if node.attr.startswith("__"):
                return False, f"forbidden dunder attribute access: {node.attr}"
            if isinstance(node.value, ast.Name) and node.value.id in os_aliases:
                if _is_forbidden_os_attr(node.attr):
                    return False, f"forbidden os mutation: {node.value.id}.{node.attr}"
            elif node.attr in (
                "system",
                "popen",
                "fork",
                "forkpty",
                "kill",
                "killpg",
                "unlink",
                "rmdir",
                "removedirs",
                "chmod",
                "chown",
                "chroot",
                "symlink",
                "symlink_to",
                "hardlink_to",
            ):
                return False, f"forbidden mutation call: {node.attr}"

        elif isinstance(node, ast.Name):
            if node.id in FORBIDDEN_BUILTINS:
                return False, f"forbidden builtin: {node.id}"

        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in FORBIDDEN_BUILTINS:
                return False, f"forbidden builtin call: {node.func.id}"

    return True, None


def _parse_last_json_object(stdout: str) -> Optional[dict[str, Any]]:
    """Extract the last JSON dict printed to stdout (tolerating log lines and fences)."""
    if not stdout or not stdout.strip():
        return None
    cleaned = re.sub(r"```(?:json)?\s*", "", stdout).replace("```", "")
    decoder = json.JSONDecoder()
    starts = [m.start() for m in re.finditer(r"\{", cleaned)]
    fallback: Optional[dict[str, Any]] = None
    for idx in reversed(starts):
        tail = cleaned[idx:].lstrip()
        try:
            obj, _ = decoder.raw_decode(tail)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            if "verdict" in obj and "finding" in obj:
                return obj
            if fallback is None:
                fallback = obj
    return fallback


def _iter_json_objects(text: str) -> list[dict[str, Any]]:
    """Every JSON dict in ``text`` in document order (tolerating prose and fences).

    Each ``{`` is a candidate start, so an object nested inside a wrapper object is
    still found; slices that are not valid JSON (e.g. braces inside a JSON string
    value, as experiment source often contains) simply fail to decode and are skipped.
    """
    if not text or not text.strip():
        return []
    cleaned = re.sub(r"```(?:json)?\s*", "", text).replace("```", "")
    decoder = json.JSONDecoder()
    objects: list[dict[str, Any]] = []
    for start in (m.start() for m in re.finditer(r"\{", cleaned)):
        try:
            obj, _ = decoder.raw_decode(cleaned, start)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            objects.append(obj)
    return objects


def _parse_spec_object(reply: str) -> Optional[dict[str, Any]]:
    """Extract the LLM's spec object: the object carrying string ``code``, else the first dict.

    The spec contract wins over any experiment-result JSON the model echoes back. Unlike
    ``_parse_last_json_object`` (for experiment stdout), this never prefers the *last*
    object: an echoed sample ``{"verdict": ..., "finding": ...}`` object then cannot
    shadow the spec and leave ``code`` empty for the AST gate.
    """
    objects = _iter_json_objects(reply)
    for obj in objects:
        if isinstance(obj.get("code"), str):
            return obj
    for obj in objects:
        if obj:
            return obj
    return objects[0] if objects else None


def execute_experiment(code: str, *, timeout: float = 10.0) -> dict[str, Any]:
    """Validate and run ``code`` inside an isolated temporary directory."""
    ok, reason = validate_code(code)
    if not ok:
        return {
            "ok": False,
            "status": "rejected",
            "error": reason,
            "returncode": None,
            "stdout": "",
            "stderr": "",
            "duration_ms": 0,
            "result": None,
        }

    t0 = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="neobrain-sandbox-") as tmp_dir:
        tmp_path = Path(tmp_dir)
        script_path = tmp_path / "experiment.py"
        script_path.write_text(code, encoding="utf-8")
        env = {
            "PATH": "/usr/bin:/bin",
            "HOME": str(tmp_path),
            "TMPDIR": str(tmp_path),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        try:
            proc = subprocess.run(
                [sys.executable, "-I", "experiment.py"],
                cwd=str(tmp_path),
                env=env,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            duration_ms = int(round((time.perf_counter() - t0) * 1000))
            stdout = (
                exc.stdout
                if isinstance(exc.stdout, str)
                else (exc.stdout.decode("utf-8", "replace") if exc.stdout else "")
            )
            stderr = (
                exc.stderr
                if isinstance(exc.stderr, str)
                else (exc.stderr.decode("utf-8", "replace") if exc.stderr else "")
            )
            return {
                "ok": False,
                "status": "timeout",
                "error": f"experiment timed out after {timeout}s",
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "result": None,
            }

        duration_ms = int(round((time.perf_counter() - t0) * 1000))
        stdout = proc.stdout or ""
        stderr = proc.stderr or ""
        if proc.returncode != 0:
            return {
                "ok": False,
                "status": "failed",
                "error": stderr.strip() or f"process exited with code {proc.returncode}",
                "returncode": proc.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "result": None,
            }

        parsed = _parse_last_json_object(stdout)
        if not isinstance(parsed, dict):
            return {
                "ok": False,
                "status": "failed",
                "error": "experiment did not print a valid JSON object to stdout",
                "returncode": 0,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "result": None,
            }

        verdict = str(parsed.get("verdict") or "").strip().lower()
        finding = parsed.get("finding")
        if verdict not in ("confirmed", "refuted", "inconclusive") or not isinstance(finding, str):
            return {
                "ok": False,
                "status": "failed",
                "error": "experiment JSON missing valid 'verdict' or 'finding'",
                "returncode": 0,
                "stdout": stdout,
                "stderr": stderr,
                "duration_ms": duration_ms,
                "result": None,
            }

        metrics = parsed.get("metrics")
        result_dict: dict[str, Any] = {
            "verdict": verdict,
            "finding": finding.strip(),
            "metrics": metrics if isinstance(metrics, dict) else {},
        }
        return {
            "ok": True,
            "status": "ok",
            "error": None,
            "returncode": 0,
            "stdout": stdout,
            "stderr": stderr,
            "duration_ms": duration_ms,
            "result": result_dict,
        }


def _ran_tonight(conn: sqlite3.Connection, now: datetime) -> bool:
    rows = conn.execute(
        "SELECT ts FROM events WHERE source=? AND title=? ORDER BY ts DESC LIMIT 30",
        (SOURCE, EVENT_TITLE),
    ).fetchall()
    target_date = now.date()
    for row in rows:
        if row[0] is None:
            continue
        if datetime.fromtimestamp(int(row[0]) / 1000).date() == target_date:
            return True
    return False


def _emit_event(
    conn: sqlite3.Connection,
    *,
    when_ms: int,
    summary: str,
    severity: str,
    detail: dict[str, Any],
) -> str:
    payload = {
        "phase": "dream sandbox",
        "next_due": when_ms + _DAY_MS,
        "summary": summary,
        **detail,
    }
    detail_json = json.dumps(payload, ensure_ascii=False)
    eid = event_id(SOURCE, f"dream-sandbox:{when_ms}")
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
                title=EVENT_TITLE,
                detail=detail_json,
                severity=severity,
                refs={"phase": "dream sandbox", "date": str(detail.get("date") or "")},
                raw=payload,
            )
        ],
    )
    return eid


def _build_prompt(wakeup: dict[str, Any], day_note: str, date: str) -> list[dict[str, str]]:
    open_loops = wakeup.get("open") or wakeup.get("open_loops") or []
    recent = wakeup.get("recent") or []
    candidate_lines: list[str] = []
    for item in list(open_loops)[:8] + list(recent)[:8]:
        if isinstance(item, dict):
            aid = item.get("id") or ""
            atype = item.get("type") or "observation"
            hub = item.get("hub") or ""
            text = (item.get("text") or item.get("label") or "").strip().replace("\n", " ")
            candidate_lines.append(f"- id={aid} [{atype}] (hub={hub}): {text[:260]}")

    candidates_block = "\n".join(candidate_lines) if candidate_lines else "- (no candidate atoms yet)"
    note_excerpt = (day_note or "").strip()[:3000] or "(no day note for this date)"

    system = (
        "You are neoBrain's Counterfactual Dream-Sandbox. Your job is to select one open "
        "question or technical hypothesis from the mind's recent memories or day note and "
        "synthesize a small, self-contained, deterministic Python 3 experiment to test it.\n"
        "Anti-toy-experiment guard:\n"
        "- If there is no concrete open question or falsifiable technical claim in the candidate "
        'memories or day note, you MUST return {"skip": true, "reason": "no-testable-hypothesis"} '
        "instead of inventing a trivial or toy test.\n"
        "Constraints on `code`:\n"
        "- Only use safe computational stdlib modules (math, statistics, time, json, re, "
        "collections, itertools, functools, hashlib, random, sqlite3, dataclasses, typing, "
        "decimal, fractions, heapq, bisect, array, struct, sys, os).\n"
        "- NEVER import networking, subprocess, shutil, ctypes, multiprocessing, signal, pty, "
        "fcntl, resource, webbrowser, pip, venv, importlib, builtins.\n"
        "- NEVER call or reference eval, exec, compile, __import__, input, breakpoint, "
        "getattr, setattr, delattr, globals, locals, vars, open, or access __dunder__ attributes.\n"
        "- NEVER call os.system, os.popen, os.spawn*, os.exec*, os.fork, os.kill, os.remove, os.unlink, etc.\n"
        "- Print a single JSON object on the last line of stdout with keys: "
        '{"verdict": "confirmed"|"refuted"|"inconclusive", "finding": "<concise empirical lesson>", '
        '"metrics": {...}}. '
        "That shape is what your experiment prints, not what you reply with: NEVER echo that "
        "example or sample output JSON back in your reply.\n"
        "Reply contract:\n"
        "- Your reply is EXACTLY ONE JSON object: the spec. Return no other JSON object and no "
        "prose around it.\n"
        "- NEVER echo example or sample output JSON (a `verdict`/`finding` result object) after "
        "the spec: an echoed sample shadows the spec, its code is lost, and the run is rejected.\n"
    )
    user = (
        f"Dream date: {date}\n\n"
        f"Candidate memories:\n{candidates_block}\n\n"
        f"Day note ({date}.md):\n{note_excerpt}\n\n"
        "Return ONLY a JSON object with keys:\n"
        '{"question": "...", "hypothesis": "...", "hub": "...", '
        '"prior_atom_id": "<atom id or null>", "code": "<python3 script>"}\n'
        "Or, if there is no concrete testable hypothesis, return ONLY:\n"
        '{"skip": true, "reason": "no-testable-hypothesis"}\n'
        "Reply with exactly ONE JSON object (the spec, or the skip object) and nothing else: never "
        "add prose and never echo example or sample output JSON after it."
    )
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]


def _write_reports(date: str, record: dict[str, Any]) -> tuple[Path, Path]:
    sandbox_dir = _dream_root() / "sandbox"
    sandbox_dir.mkdir(parents=True, exist_ok=True)
    json_path = sandbox_dir / f"{date}.json"
    md_path = sandbox_dir / f"{date}.md"

    json_path.write_text(json.dumps(record, indent=2, ensure_ascii=False), encoding="utf-8")

    verdict = record.get("verdict") or record.get("status") or "inconclusive"
    finding = record.get("finding") or record.get("error") or "(no finding)"
    metrics_json = json.dumps(record.get("metrics") or {}, indent=2, ensure_ascii=False)
    md_lines = [
        f"# Dream Sandbox — {date}",
        "",
        f"- **Question:** {record.get('question') or ''}",
        f"- **Hypothesis:** {record.get('hypothesis') or ''}",
        f"- **Hub:** {record.get('hub') or 'agent'}",
        f"- **Prior Atom:** {record.get('prior_atom_id') or 'none'}",
        f"- **Status:** {record.get('status') or ''}",
        f"- **Verdict:** {verdict}",
        f"- **Atom ID:** {record.get('atom_id') or 'none'}",
        "",
        "## Finding",
        "",
        str(finding),
        "",
        "## Metrics",
        "",
        "```json",
        metrics_json,
        "```",
        "",
    ]
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    return json_path, md_path


def run(
    conn: sqlite3.Connection,
    runtime: Any,
    *,
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    """Run one nightly counterfactual sandbox experiment and promote confirmed/refuted lessons."""
    now_dt = now or datetime.now()
    date = _dream_date(now_dt)
    when_ms = int(now_dt.timestamp() * 1000)

    if _ran_tonight(conn, now_dt):
        return {"status": "skipped", "reason": "already-ran-tonight", "date": date}

    try:
        wakeup = mind.wake_up(conn, session_id="sandbox")
    except Exception:  # noqa: BLE001
        wakeup = {}

    day_note = ""
    note_path = _day_note_path(date)
    if note_path.exists():
        try:
            day_note = note_path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            day_note = ""

    messages = _build_prompt(wakeup, day_note, date)

    raw_reply = ""
    llm_error: Optional[str] = None
    for tier in ("strong", "cheap"):
        try:
            raw_reply = (
                runtime.chat(
                    messages,
                    model=tier,
                    json_mode=True,
                )
                or ""
            ).strip()
            if raw_reply:
                llm_error = None
                break
        except Exception as exc:  # noqa: BLE001
            llm_error = f"{type(exc).__name__}: {exc}"

    if not raw_reply:
        err_msg = llm_error or "empty response from LLM"
        out = {
            "status": "error",
            "date": date,
            "error": err_msg,
            "verdict": None,
            "atom_id": None,
        }
        try:
            _emit_event(
                conn,
                when_ms=when_ms,
                summary=f"dream sandbox failed ({date}): {err_msg}",
                severity="error",
                detail=out,
            )
        except Exception:  # noqa: BLE001
            pass
        return out

    spec = _parse_spec_object(raw_reply)
    if not isinstance(spec, dict):
        try:
            spec = json.loads(raw_reply)
        except Exception as exc:  # noqa: BLE001
            err_msg = f"invalid LLM JSON spec: {exc}"
            out = {
                "status": "error",
                "date": date,
                "error": err_msg,
                "verdict": None,
                "atom_id": None,
            }
            try:
                _emit_event(
                    conn,
                    when_ms=when_ms,
                    summary=f"dream sandbox failed ({date}): {err_msg}",
                    severity="error",
                    detail=out,
                )
            except Exception:  # noqa: BLE001
                pass
            return out

    if bool(spec.get("skip")) is True or str(spec.get("status") or "").strip().lower() in (
        "skip",
        "skipped",
    ):
        reason = str(spec.get("reason") or "").strip() or "no-testable-hypothesis"
        skip_out: dict[str, Any] = {
            "status": "skipped",
            "reason": reason,
            "date": date,
            "atom_id": None,
        }
        try:
            _emit_event(
                conn,
                when_ms=when_ms,
                summary=f"sandbox ({date}): skipped — {reason}",
                severity="info",
                detail=skip_out,
            )
        except Exception:  # noqa: BLE001
            pass
        return skip_out

    question = str(spec.get("question") or "").strip()
    hypothesis = str(spec.get("hypothesis") or "").strip()
    hub = str(spec.get("hub") or "").strip()
    prior_atom_id = spec.get("prior_atom_id")
    if isinstance(prior_atom_id, str):
        prior_atom_id = prior_atom_id.strip() or None
    else:
        prior_atom_id = None
    code = str(spec.get("code") or "")

    timeout = float(getattr(config.settings, "sandbox_timeout_seconds", 10) or 10)
    exec_res = execute_experiment(code, timeout=timeout)

    res_obj = exec_res.get("result") if isinstance(exec_res.get("result"), dict) else None
    verdict = res_obj.get("verdict") if res_obj else None
    finding = str(res_obj.get("finding") or "").strip() if res_obj else ""
    metrics = res_obj.get("metrics") if (res_obj and isinstance(res_obj.get("metrics"), dict)) else {}

    atom_id: Optional[str] = None
    if exec_res.get("ok") and verdict in ("confirmed", "refuted") and finding:
        try:
            atom = mind.remember(
                conn,
                finding,
                atype="lesson",
                hubs=[hub] if hub else ["agent"],
                source=f"memory/dreaming/sandbox/{date}.md",
                session_id="sandbox",
                dedupe=True,
            )
            if isinstance(atom, dict) and atom.get("id"):
                atom_id = str(atom["id"])
                if prior_atom_id:
                    exists = conn.execute(
                        "SELECT 1 FROM m_atoms WHERE id=?", (prior_atom_id,)
                    ).fetchone()
                    if exists:
                        mind.link(
                            conn,
                            atom_id,
                            prior_atom_id,
                            "derived-from",
                            session_id="sandbox",
                            explicit=False,
                        )
        except Exception:  # noqa: BLE001
            atom_id = None

    status = "ok" if exec_res.get("ok") else str(exec_res.get("status") or "failed")
    record: dict[str, Any] = {
        "date": date,
        "question": question,
        "hypothesis": hypothesis,
        "hub": hub or "agent",
        "prior_atom_id": prior_atom_id,
        "status": status,
        "ok": bool(exec_res.get("ok")),
        "duration_ms": exec_res.get("duration_ms", 0),
        "returncode": exec_res.get("returncode"),
        "error": exec_res.get("error"),
        "result": res_obj,
        "verdict": verdict,
        "finding": finding,
        "metrics": metrics,
        "atom_id": atom_id,
    }

    try:
        _write_reports(date, record)
    except Exception:  # noqa: BLE001
        pass

    summary = (
        f"sandbox ({date}): {verdict} — {finding[:140]}"
        if exec_res.get("ok")
        else f"sandbox ({date}): {status} — {exec_res.get('error') or ''}"
    )
    try:
        _emit_event(
            conn,
            when_ms=when_ms,
            summary=summary,
            severity="info" if exec_res.get("ok") else "warning",
            detail=record,
        )
    except Exception:  # noqa: BLE001
        pass

    return {
        "status": status,
        "date": date,
        "question": question,
        "hypothesis": hypothesis,
        "hub": hub or "agent",
        "prior_atom_id": prior_atom_id,
        "verdict": verdict,
        "finding": finding,
        "metrics": metrics,
        "duration_ms": exec_res.get("duration_ms", 0),
        "atom_id": atom_id,
        "error": exec_res.get("error"),
    }


def history(conn: sqlite3.Connection, limit: int = 30) -> list[dict[str, Any]]:
    """List recent `life: dream sandbox` events with parsed detail."""
    rows = conn.execute(
        "SELECT id, ts, source, lane, category, actor, title, severity, detail "
        "FROM events WHERE title=? ORDER BY ts DESC LIMIT ?",
        (EVENT_TITLE, int(limit)),
    ).fetchall()
    items: list[dict[str, Any]] = []
    for r in rows:
        row_dict = dict(r)
        parsed_detail: dict[str, Any] = {}
        raw_detail = row_dict.get("detail")
        if isinstance(raw_detail, str) and raw_detail:
            try:
                loaded = json.loads(raw_detail)
                if isinstance(loaded, dict):
                    parsed_detail = loaded
            except json.JSONDecodeError:
                parsed_detail = {}
        elif isinstance(raw_detail, dict):
            parsed_detail = raw_detail

        items.append(
            {
                "id": row_dict.get("id"),
                "ts": row_dict.get("ts"),
                "ts_iso": iso(row_dict.get("ts")),
                "source": row_dict.get("source"),
                "lane": row_dict.get("lane"),
                "category": row_dict.get("category"),
                "actor": row_dict.get("actor"),
                "title": row_dict.get("title"),
                "severity": row_dict.get("severity"),
                "detail": parsed_detail,
                "date": parsed_detail.get("date"),
                "status": parsed_detail.get("status"),
                "verdict": parsed_detail.get("verdict"),
                "question": parsed_detail.get("question"),
                "hypothesis": parsed_detail.get("hypothesis"),
                "finding": parsed_detail.get("finding"),
                "atom_id": parsed_detail.get("atom_id"),
                "summary": parsed_detail.get("summary"),
            }
        )
    return items


def roi_report(
    conn: sqlite3.Connection,
    *,
    days: int = 7,
    at_ms: Optional[int] = None,
) -> dict[str, Any]:
    """Compute the empirical ROI scorecard for sandbox runs and lessons over ``days``."""
    days_clamped = max(1, int(days))
    end_ms = int(at_ms) if at_ms is not None else int(time.time() * 1000)
    start_ms = end_ms - days_clamped * _DAY_MS

    runs: dict[str, int] = {
        "total": 0,
        "ok": 0,
        "skipped": 0,
        "failed": 0,
        "confirmed": 0,
        "refuted": 0,
        "inconclusive": 0,
    }
    ev_rows = conn.execute(
        "SELECT detail FROM events WHERE title=? AND ts>=? AND ts<=? ORDER BY ts DESC",
        (EVENT_TITLE, start_ms, end_ms),
    ).fetchall()
    for r in ev_rows:
        runs["total"] += 1
        raw_detail = r[0]
        detail: dict[str, Any] = {}
        if isinstance(raw_detail, str) and raw_detail:
            try:
                loaded = json.loads(raw_detail)
                if isinstance(loaded, dict):
                    detail = loaded
            except json.JSONDecodeError:
                detail = {}
        elif isinstance(raw_detail, dict):
            detail = raw_detail

        st = str(detail.get("status") or "").strip().lower()
        verdict = str(detail.get("verdict") or "").strip().lower()
        if st == "ok":
            runs["ok"] += 1
        elif st in ("skipped", "skip"):
            runs["skipped"] += 1
        else:
            runs["failed"] += 1

        if verdict in ("confirmed", "refuted", "inconclusive"):
            runs[verdict] += 1

    atom_cols = {row[1] for row in conn.execute("PRAGMA table_info(m_atoms)").fetchall()}
    # Only promoted lessons count: the nightly ingest also files each sandbox report as a
    # type='dream' atom under the same sandbox provenance, which must not inflate the ROI.
    predicates = ["source LIKE 'memory/dreaming/sandbox/%'"]
    if "session_id" in atom_cols:
        predicates.append("session_id = 'sandbox'")
    if "origin_session_id" in atom_cols:
        predicates.append("origin_session_id = 'sandbox'")
    where_sandbox = "(" + " OR ".join(predicates) + ")"

    atom_rows = conn.execute(
        f"SELECT id, label, text, hub, created, source FROM m_atoms "
        f"WHERE created>=? AND created<=? AND type = 'lesson' AND {where_sandbox} "
        f"ORDER BY created DESC, id ASC",
        (start_ms, end_ms),
    ).fetchall()

    lessons: list[dict[str, Any]] = []
    total_served = 0
    total_used = 0
    total_useful = 0
    total_noise = 0
    total_unused = 0

    for a in atom_rows:
        ad = dict(a)
        aid = str(ad["id"])
        rank_row = conn.execute(
            "SELECT served FROM m_rank WHERE atom_id=?", (aid,)
        ).fetchone()
        served_n = int(rank_row[0] or 0) if rank_row else 0

        fb_counts = {"used": 0, "useful": 0, "noise": 0, "unused": 0}
        fb_rows = conn.execute(
            "SELECT signal, COUNT(*) FROM m_feedback WHERE atom_id=? GROUP BY signal",
            (aid,),
        ).fetchall()
        for sig, cnt in fb_rows:
            sig_key = str(sig or "").strip().lower()
            if sig_key in fb_counts:
                fb_counts[sig_key] = int(cnt or 0)

        total_served += served_n
        total_used += fb_counts["used"]
        total_useful += fb_counts["useful"]
        total_noise += fb_counts["noise"]
        total_unused += fb_counts["unused"]

        lessons.append(
            {
                "id": aid,
                "label": ad.get("label") or "",
                "text": ad.get("text") or "",
                "hub": ad.get("hub") or "",
                "created": int(ad["created"]) if ad.get("created") is not None else 0,
                "source": ad.get("source") or "",
                "served": served_n,
                "used": fb_counts["used"],
                "useful": fb_counts["useful"],
                "noise": fb_counts["noise"],
                "unused": fb_counts["unused"],
            }
        )

    return {
        "days": days_clamped,
        "runs": runs,
        "lessons_created": len(lessons),
        "served": total_served,
        "used": total_used,
        "useful": total_useful,
        "noise": total_noise,
        "unused": total_unused,
        "lessons": lessons,
    }
