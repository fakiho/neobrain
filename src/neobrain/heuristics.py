"""Heuristic extraction of decisions and severity from free text.

Rules are intentionally simple, deterministic and overridable. LLM enrichment
(Stage B3) refines titles/summaries later; it never replaces the raw text.
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

# heading / marker -> (lane, category, severity)
_HEADING_RULES: list[Tuple[re.Pattern, str, str, str]] = [
    (re.compile(r"\b(root\s*cause|cause\s*was|why\b)", re.I), "decision", "root-cause", "warning"),
    (re.compile(r"\b(lesson|lessons)\b", re.I), "decision", "lesson", "notice"),
    (re.compile(r"\b(revert(ed)?|rollback)\b", re.I), "decision", "revert", "warning"),
    (re.compile(r"\b(fix|fixed|applied|resolved|confirmed|working)\b", re.I), "decision", "change", "success"),
    (re.compile(r"\b(change[sd]?|implemented|built|created|added|removed|installed)\b", re.I), "decision", "change", "success"),
    (re.compile(r"\b(decision|decided|design agreed|agreed|chose|chosen|plan)\b", re.I), "decision", "decision", "notice"),
    (re.compile(r"\b(still open|open items?|todo|pending|next steps?)\b", re.I), "decision", "open", "notice"),
    (re.compile(r"\b(goal|objective|vision)\b", re.I), "decision", "goal", "notice"),
]

# headings that should NOT be treated as decisions even if they match above
_HEADING_DENY = re.compile(r"\b(see also|references?|related|requirements|notes?|summary|overview)\b", re.I)

_INLINE_MARKERS = re.compile(
    r"\b(root\s*cause|lesson(?:\s+learned)?|we (?:decided|chose)|i (?:decided|chose)|"
    r"the fix (?:is|was)|we(?:'ll| will) use|confirmed|reverted|applied|"
    r"the cause (?:is|was)|this (?:is|was) the (?:fix|cause))\b",
    re.I,
)

_ERROR_HINTS = re.compile(
    r"\b(error|failed|failure|exception|traceback|panic|timeout|denied|refused|"
    r"unreachable|no route to host)\b",
    re.I,
)
_SUCCESS_HINTS = re.compile(r"\b(ok|success|fixed|applied|verified|completed|healthy|works)\b", re.I)


def classify_heading(heading: str) -> Optional[Tuple[str, str, str]]:
    """Return (lane, category, severity) for a Markdown heading, or None."""
    if not heading:
        return None
    h = heading.strip().strip("#").strip()
    if not h or _HEADING_DENY.search(h):
        return None
    for pattern, lane, category, severity in _HEADING_RULES:
        if pattern.search(h):
            return lane, category, severity
    return None


def is_decision_text(text: str) -> bool:
    return bool(text and _INLINE_MARKERS.search(text))


def severity_for(text: str, default: str = "info") -> str:
    if not text:
        return default
    if _ERROR_HINTS.search(text) and not _SUCCESS_HINTS.search(text):
        return "error"
    if _SUCCESS_HINTS.search(text):
        return "success"
    return default


def severity_for_tool(status: str, output: str = "") -> str:
    s = (status or "").lower()
    if s in ("error", "failed", "failure"):
        return "error"
    if _ERROR_HINTS.search(output or "") and not _SUCCESS_HINTS.search(output or ""):
        return "error"
    if s in ("completed", "success", "ok"):
        return "info"
    return "notice"


def tags_for(heading: str) -> list[str]:
    tags: list[str] = []
    h = (heading or "").lower()
    for kw in ("dns", "dhcp", "network", "ipv6", "wifi", "frigate", "grafana", "influx",
               "opencode", "openwrt", "litellm", "snort", "adguard", "docker", "systemd",
               "memory", "camera", "homekit", "matter", "zigbee", "security", "npm"):
        if kw in h:
            tags.append(kw)
    return tags
