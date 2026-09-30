"""OpenCode session-DB adapter.

PORT-NOTE: verbatim port of ``timeline/app/ingest/opencode.py`` (event mapping
and shapes unchanged). Two deviations, both marked inline:

* imports use the ``neobrain.*`` package;
* ``iter_events`` treats a missing **or None** ``config.OPENCODE_DB`` as "no
  OpenCode source" and returns ``([], {})`` cleanly — the old code called
  ``Path(config.OPENCODE_DB)`` unconditionally and NEOBRAIN_OPENCODE_DB defaults
  to None when no workspace is configured.

Reads the live OpenCode SQLite store read-only and turns it into timeline
events: user requests, agent actions (tool calls), decisions (assistant text),
rationale (reasoning) and system/model notices.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Iterable, Optional

from .. import config
from ..heuristics import is_decision_text, severity_for, severity_for_tool
from ..models import Event, content_hash, first_line, new

# tool name -> (lane, category)
TOOL_MAP: dict[str, tuple[str, str]] = {
    "shell": ("action", "shell"),
    "edit": ("file", "edit"),
    "write": ("file", "write"),
    "read": ("file", "read"),
    "grep": ("file", "read"),
    "webfetch": ("research", "webfetch"),
    "websearch": ("research", "websearch"),
    "question": ("question", "question"),
    "skill": ("action", "skill"),
    "execute": ("action", "execute"),
}

MAX_DETAIL = 4000


def _truncate(text: Optional[str], limit: int = MAX_DETAIL) -> Optional[str]:
    if text is None:
        return None
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def _open_ro() -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{config.OPENCODE_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def read_sessions(conn: sqlite3.Connection) -> list[dict]:
    out = []
    for r in conn.execute(
        """SELECT id, project_id, directory, title, agent, model, cost,
                  time_created, time_updated FROM session_v2"""
    ):
        out.append(
            {
                "id": r["id"],
                "project_id": r["project_id"],
                "directory": r["directory"],
                "title": r["title"],
                "agent": r["agent"],
                "model": r["model"],
                "cost": r["cost"],
                "ts_created": r["time_created"],
                "ts_updated": r["time_updated"],
            }
        )
    return out


def _tool_event(
    *,
    session_id: str,
    message_id: str,
    model: str,
    part: dict,
    project_id: Optional[str],
    msg_ts: int,
) -> Optional[Event]:
    name = part.get("name") or "unknown"
    lane, category = TOOL_MAP.get(name, ("action", name))
    state = part.get("state") or {}
    t = part.get("time") or {}
    ts = t.get("created") or t.get("ran") or t.get("completed") or msg_ts
    inp = state.get("input") or {}
    if not isinstance(inp, dict):
        inp = {"raw": inp}
    status = state.get("status") or ""
    content = state.get("content")
    output_text = ""
    if isinstance(content, list):
        output_text = "\n".join(
            c.get("text", "") for c in content if isinstance(c, dict)
        )

    refs: dict = {
        "tool": name,
        "session_id": session_id,
        "message_id": message_id,
        "part_id": part.get("id"),
    }
    title = name
    detail = ""
    if name == "shell":
        cmd = inp.get("command", "")
        title = first_line(cmd, 160) or "shell"
        detail = cmd
        refs["command"] = first_line(cmd, 300)
    elif name in ("edit",):
        path = inp.get("path", "")
        refs["path"] = path
        title = f"edit {Path(path).name}"
        detail = _truncate(
            f"--- old\n{inp.get('oldString','')}\n+++ new\n{inp.get('newString','')}"
        )
    elif name == "write":
        path = inp.get("path", "")
        refs["path"] = path
        title = f"write {Path(path).name}"
        detail = _truncate(inp.get("content", ""))
    elif name in ("read",):
        path = inp.get("path", "")
        refs["path"] = path
        title = f"read {Path(path).name}"
        detail = path
    elif name == "grep":
        refs["pattern"] = inp.get("pattern", "")
        refs["path"] = inp.get("path", "")
        title = f"grep {first_line(inp.get('pattern',''), 80)}"
        detail = f"pattern={inp.get('pattern','')} path={inp.get('path','')}"
    elif name == "webfetch":
        refs["url"] = inp.get("url", "")
        title = first_line(inp.get("url", ""), 160)
        detail = inp.get("url", "")
    elif name == "websearch":
        refs["query"] = inp.get("query", "")
        title = f"search: {first_line(inp.get('query',''), 120)}"
        detail = inp.get("query", "")
    elif name == "question":
        qs = inp.get("questions") or []
        first_q = ""
        if qs and isinstance(qs, list) and isinstance(qs[0], dict):
            first_q = qs[0].get("question", "")
        title = f"asked: {first_line(first_q, 120)}"
        detail = json.dumps(qs, ensure_ascii=False)[:MAX_DETAIL]
    elif name == "skill":
        refs["skill"] = inp.get("id", "")
        title = f"skill {inp.get('id','')}"
    elif name == "execute":
        title = "execute (code mode)"
        detail = _truncate(inp.get("code", ""))
    else:
        detail = json.dumps(inp, ensure_ascii=False)[:MAX_DETAIL]

    output_snippet = _truncate(output_text, 2000)
    ev = new(
        ts=ts,
        source="opencode",
        lane=lane,
        category=category,
        actor=f"agent:{model}" if model else "agent",
        title=title,
        source_ref=f"{session_id}:{message_id}:tool:{part.get('id')}",
        detail=detail,
        status=status or None,
        severity=severity_for_tool(status, output_text),
        project_id=project_id,
        session_id=session_id,
        refs=refs,
        raw={"tool": name, "input": inp, "output": output_snippet, "status": status},
    )
    return ev


def iter_events() -> tuple[list[Event], dict]:
    """Yield all OpenCode events + session metadata."""
    # PORT-NOTE: None (no workspace configured) and a missing file both mean
    # "this source has nothing to offer" — return empty instead of crashing.
    if config.OPENCODE_DB is None or not Path(config.OPENCODE_DB).exists():
        return [], {}
    conn = _open_ro()
    sessions = read_sessions(conn)
    proj_by_session = {s["id"]: s["project_id"] for s in sessions}
    events: list[Event] = []
    link_intents: list[tuple[str, str, str]] = []

    for r in conn.execute(
        "SELECT id, session_id, type, time_created, data FROM session_message "
        "ORDER BY session_id, seq, time_created"
    ):
        mid = r["id"]
        sid = r["session_id"]
        mtype = r["type"]
        ts = r["time_created"]
        project_id = proj_by_session.get(sid)
        try:
            data = json.loads(r["data"])
        except (json.JSONDecodeError, TypeError):
            continue

        if mtype == "user":
            text = data.get("text", "")
            events.append(
                new(
                    ts=data.get("time", {}).get("created", ts),
                    source="opencode",
                    lane="request",
                    category="request",
                    actor="user",
                    title=first_line(text, 160) or "user message",
                    source_ref=f"{sid}:{mid}:user",
                    detail=_truncate(text),
                    project_id=project_id,
                    session_id=sid,
                    refs={"files": data.get("files", [])},
                    raw={"text": _truncate(text, 2000)},
                )
            )
            continue

        if mtype == "assistant":
            model = (data.get("model") or {}).get("id", "")
            content = data.get("content") or []
            msg_ts = data.get("time", {}).get("created", ts)
            rationale_text = "\n\n".join(
                p.get("text", "")
                for p in content
                if isinstance(p, dict) and p.get("type") == "reasoning" and p.get("text")
            )
            created: list[Event] = []
            decision_evs: list[Event] = []
            for idx, part in enumerate(content):
                if not isinstance(part, dict):
                    continue
                ptype = part.get("type")
                if ptype == "tool":
                    ev = _tool_event(
                        session_id=sid,
                        message_id=mid,
                        model=model,
                        part=part,
                        project_id=project_id,
                        msg_ts=msg_ts,
                    )
                    if ev:
                        created.append(ev)
                elif ptype == "text":
                    text = part.get("text", "")
                    if text and is_decision_text(text):
                        dev = new(
                            ts=msg_ts,
                            source="opencode",
                            lane="decision",
                            category="statement",
                            actor=f"agent:{model}" if model else "agent",
                            title=first_line(text, 160),
                            source_ref=f"{sid}:{mid}:text:{idx}",
                            detail=_truncate(text),
                            severity=severity_for(text),
                            project_id=project_id,
                            session_id=sid,
                            refs={"kind": "statement"},
                            raw={"text": _truncate(text, 2000)},
                        )
                        created.append(dev)
                        decision_evs.append(dev)

            rationale_ev: Optional[Event] = None
            if rationale_text and len(rationale_text) > 80 and created:
                rationale_ev = new(
                    ts=msg_ts - 1,
                    source="opencode",
                    lane="notice",
                    category="rationale",
                    actor=f"agent:{model}" if model else "agent",
                    title=first_line(rationale_text, 160),
                    source_ref=f"{sid}:{mid}:reasoning",
                    detail=_truncate(rationale_text),
                    severity="info",
                    project_id=project_id,
                    session_id=sid,
                    refs={"kind": "reasoning"},
                    raw={"text": _truncate(rationale_text, 2000)},
                )
                created.append(rationale_ev)

            events.extend(created)
            # rationale -> every action/decision produced in this message
            if rationale_ev is not None:
                for ev in created:
                    if ev is rationale_ev or not ev.source_ref:
                        continue
                    link_intents.append((rationale_ev.source_ref, ev.source_ref, "rationale"))
            continue

        if mtype == "system":
            text = data.get("text", "") or data.get("description", "")
            events.append(
                new(
                    ts=data.get("time", {}).get("created", ts),
                    source="opencode",
                    lane="notice",
                    category="system",
                    actor="system",
                    title=first_line(text, 160) or "system",
                    source_ref=f"{sid}:{mid}:system",
                    detail=_truncate(text),
                    project_id=project_id,
                    session_id=sid,
                    raw={"text": text},
                )
            )
            continue

        if mtype == "model-switched":
            mdl = data.get("model") or {}
            prev = data.get("previous") or {}
            events.append(
                new(
                    ts=data.get("time", {}).get("created", ts),
                    source="opencode",
                    lane="notice",
                    category="model",
                    actor="user",
                    title=(
                        f"model switched: {prev.get('id','?')}"
                        f"#{prev.get('variant','')} -> {mdl.get('id','?')}#{mdl.get('variant','')}"
                    ),
                    source_ref=f"{sid}:{mid}:model",
                    project_id=project_id,
                    session_id=sid,
                    raw=data,
                )
            )
            continue

        if mtype == "synthetic":
            text = data.get("text", "")
            events.append(
                new(
                    ts=data.get("time", {}).get("created", ts),
                    source="opencode",
                    lane="notice",
                    category="synthetic",
                    actor="system",
                    title=first_line(text, 160) or "synthetic",
                    source_ref=f"{sid}:{mid}:synthetic",
                    detail=_truncate(text),
                    project_id=project_id,
                    session_id=sid,
                    raw={"text": _truncate(text, 2000)},
                )
            )
            continue

    conn.close()
    return events, {"sessions": sessions, "links": link_intents}
