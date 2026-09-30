"""Docs adapter — memory notes, INFRA docs, identity files and the dream diary.

PORT-NOTE: verbatim port of ``timeline/app/ingest/docs.py``. Only the package
imports changed (``app.*`` → ``neobrain.*``); the parsing logic, event shapes,
ids and heuristics are unchanged.

Emits:
  * one `doc`/`dream` version event per file content version (history accumulates
    as the hash changes), backed by a `doc_versions` row;
  * `decision` events for sections whose heading matches the heuristics.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Iterable, Optional

from .. import config
from ..heuristics import classify_heading, tags_for
from ..models import Event, content_hash, event_id, first_line, new

_HEADING = re.compile(r"^(#{2,3})\s+(.*\S)\s*$")


def _slug(text: str, limit: int = 60) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:limit] or "section"


def sections(text: str) -> list[tuple[str, str]]:
    """Split markdown into (heading, body) for level-2/3 headings."""
    out: list[tuple[str, str]] = []
    current: Optional[str] = None
    buf: list[str] = []
    for line in text.splitlines():
        m = _HEADING.match(line)
        if m:
            if current is not None:
                out.append((current, "\n".join(buf).strip()))
            current = m.group(2).strip()
            buf = []
        elif current is not None:
            buf.append(line)
    if current is not None:
        out.append((current, "\n".join(buf).strip()))
    return out


def _doc_version_id(path: Path, h: str) -> str:
    return event_id("docver", f"{path}|{h}")


def iter_events(docs: Iterable[tuple[str, Path]] | None = None) -> tuple[list[Event], list[dict]]:
    docs = list(docs) if docs is not None else config.all_docs()
    events: list[Event] = []
    versions: list[dict] = []

    for category, path in docs:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        h = content_hash(text)
        ts = int(path.stat().st_mtime * 1000)
        vid = _doc_version_id(path, h)
        versions.append(
            {
                "id": vid,
                "doc_path": str(path),
                "ts": ts,
                "content_hash": h,
                "content": text,
                "diff": None,
            }
        )

        summary = first_line(next((s for s in sections(text) if s[0]), ("", ""))[1], 300)
        lane = "dream" if category == "dream" else "doc"
        events.append(
            new(
                ts=ts,
                source="docs",
                lane=lane,
                category=category,
                actor="agent",
                title=f"{category}: {path.name}",
                source_ref=f"{path}:{h}",
                detail=f"{path}\n\n{summary}",
                severity="info",
                refs={
                    "path": str(path),
                    "category": category,
                    "content_hash": h,
                    "doc_version_id": vid,
                },
                raw={"path": str(path), "length": len(text)},
            )
        )

        for heading, body in sections(text):
            rule = classify_heading(heading)
            if not rule:
                continue
            lane2, cat2, sev = rule
            events.append(
                new(
                    ts=ts,
                    source="docs",
                    lane=lane2,
                    category=cat2,
                    actor="agent",
                    title=heading,
                    source_ref=f"{path}:{h}:{_slug(heading)}",
                    detail=(body[:4000] + ("…" if len(body) > 4000 else "")) if body else heading,
                    severity=sev,
                    refs={
                        "path": str(path),
                        "heading": heading,
                        "category": category,
                        "tags": tags_for(heading),
                        "doc_version_id": vid,
                    },
                    raw={"heading": heading, "body": body[:2000]},
                )
            )
    return events, versions


def versions_from_writes(opencode_events: Iterable[Event], docs: Iterable[tuple[str, Path]] | None = None) -> list[dict]:
    """Reconstruct past doc versions from OpenCode `write` tool payloads."""
    doc_paths = {str(p): cat for cat, p in (list(docs) if docs is not None else config.all_docs())}
    out: list[dict] = []
    for ev in opencode_events:
        if ev.category != "write":
            continue
        path = ev.refs.get("path")
        if not path or path not in doc_paths:
            continue
        raw = ev.raw or {}
        inp = raw.get("input") if isinstance(raw, dict) else None
        content = (inp or {}).get("content") if isinstance(inp, dict) else None
        if not content:
            continue
        h = content_hash(content)
        out.append(
            {
                "id": _doc_version_id(Path(path), h),
                "doc_path": path,
                "ts": ev.ts,
                "content_hash": h,
                "content": content,
                "diff": None,
            }
        )
    return out
