"""Event model and small helpers shared by the adapters."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional

LANES = (
    "request",
    "decision",
    "action",
    "file",
    "research",
    "question",
    "git",
    "doc",
    "dream",
    "notice",
)

SEVERITIES = ("info", "notice", "success", "warning", "error")


def now_ms() -> int:
    return int(datetime.now(tz=timezone.utc).timestamp() * 1000)


def event_id(source: str, source_ref: str) -> str:
    return hashlib.sha1(f"{source}|{source_ref}".encode("utf-8")).hexdigest()


def content_hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def to_ms(value: Any) -> Optional[int]:
    """Coerce an int(ms), float, datetime or ISO string to epoch ms."""
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        v = float(value)
        return int(v if v > 1e12 else v * 1000)
    if isinstance(value, datetime):
        dt = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return int(dt.timestamp() * 1000)
        except ValueError:
            return None
    return None


def iso(ms: Optional[int]) -> str:
    if not ms:
        return ""
    return (
        datetime.fromtimestamp(ms / 1000)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S")
    )


def first_line(text: Optional[str], limit: int = 160) -> str:
    if not text:
        return ""
    line = text.strip().splitlines()[0].strip() if text.strip() else ""
    line = re.sub(r"\s+", " ", line)
    return line[:limit]


@dataclass
class Event:
    ts: int
    source: str
    lane: str
    category: str
    actor: str
    title: str
    id: str = ""
    ts_end: Optional[int] = None
    detail: Optional[str] = None
    status: Optional[str] = None
    severity: str = "info"
    project_id: Optional[str] = None
    session_id: Optional[str] = None
    refs: dict = field(default_factory=dict)
    raw: Optional[dict] = None
    source_ref: str = ""

    def finalize(self) -> "Event":
        if self.severity not in SEVERITIES:
            self.severity = "info"
        if not self.id:
            self.id = event_id(self.source, self.source_ref or f"{self.ts}:{self.title}")
        return self

    def as_row(self, ingested_at: int) -> dict:
        self.finalize()
        return {
            "id": self.id,
            "ts": self.ts,
            "ts_end": self.ts_end,
            "source": self.source,
            "lane": self.lane,
            "category": self.category,
            "actor": self.actor,
            "title": self.title,
            "detail": self.detail,
            "status": self.status,
            "severity": self.severity,
            "project_id": self.project_id,
            "session_id": self.session_id,
            "refs": json.dumps(self.refs, ensure_ascii=False),
            "raw": json.dumps(self.raw, ensure_ascii=False) if self.raw is not None else None,
            "ingested_at": ingested_at,
        }


def new(
    *,
    ts: Any,
    source: str,
    lane: str,
    category: str,
    actor: str,
    title: str,
    source_ref: str,
    **kwargs: Any,
) -> Event:
    ms = to_ms(ts) or now_ms()
    return Event(
        ts=ms,
        source=source,
        lane=lane,
        category=category,
        actor=actor,
        title=first_line(title, 400),
        source_ref=source_ref,
        **kwargs,
    )
