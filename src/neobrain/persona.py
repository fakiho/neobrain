"""Starter persona files, and the one path allowed to edit them.

``neobrain init`` seeds IDENTITY.md and SOUL.md as *placeholders* — filled
fields become identity atoms, values wrapped in parentheses are skipped by the
parser, and the weekly soul-reflect treats a placeholder as "fill me in".
Existing files are never touched, so a hand-written persona survives re-init.

The Observatory edits the same two files through ``save_persona``. Only those
two names, only inside the persona directory, and a SOUL.md edit must keep
every existing ``##`` section (the same red line the reflect runner enforces).
"""
from __future__ import annotations

from pathlib import Path

from . import config, dreams

# Unfilled values stay in parentheses: the identity parser skips those, so a
# fresh install shows the shape of an identity without inventing one.
IDENTITY_TEMPLATE = """\
# Identity

A placeholder. Fill the fields in (or edit this file from the Identity tab) —
the weekly reflection fills in whatever you leave blank.

- Name: (pick a name)
- Creature: (what kind of thing are you?)
- Vibe: (how do you come across?)
- Emoji: (one emoji)
- Avatar: (a short visual description, or a path)
- Theme: (a colour or a motif)
"""

SOUL_TEMPLATE = """\
# Soul

A placeholder. The weekly reflection evolves this from what actually happens;
edit it directly whenever you already know a truth or a limit.

## Core Truths

- (the one thing that should stay true about how you work)

## Boundaries

- (a line you do not cross)
"""

# The only files the UI may write. USER.md and AGENTS.md are user-owned ledgers.
EDITABLE = ("IDENTITY.md", "SOUL.md")


def persona_dir() -> Path | None:
    """Where the persona files live, or None when no workspace is configured."""
    if config.WORKSPACE is None:
        return None
    return dreams._persona_dir()


def seed(directory: Path | None = None) -> list[str]:
    """Write the placeholder persona files that are missing. Never overwrites.

    Returns the names written. A missing workspace is a no-op, not an error —
    init must succeed on a host with no docs configured.
    """
    directory = directory if directory is not None else persona_dir()
    if directory is None:
        return []
    directory.mkdir(parents=True, exist_ok=True)
    written: list[str] = []
    for name, template in (("IDENTITY.md", IDENTITY_TEMPLATE), ("SOUL.md", SOUL_TEMPLATE)):
        path = directory / name
        if path.exists():
            continue
        path.write_text(template, encoding="utf-8")
        written.append(name)
    return written


def save(name: str, content: str) -> Path:
    """Replace one persona file, refusing anything outside the two editable names.

    Raises ValueError for a bad name, a SOUL.md that drops a section, or a host
    with no workspace. The caller versions the result so the reader shows it.
    """
    if name not in EDITABLE:
        raise ValueError(f"{name} is not editable here — only {', '.join(EDITABLE)}")
    directory = persona_dir()
    if directory is None:
        raise ValueError("no workspace configured (NEOBRAIN_WORKSPACE) — nowhere to write")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    text = content if content.endswith("\n") else content + "\n"
    if name == "SOUL.md" and path.exists():
        current = path.read_text(encoding="utf-8", errors="replace")
        if not dreams._preserves_sections(current, text):
            raise ValueError("SOUL.md must keep every existing ## section (Boundaries, Core Truths, …)")
    path.write_text(text, encoding="utf-8")
    return path
