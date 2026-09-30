"""Git history adapter — one timeline event per commit across all repos.

PORT-NOTE: verbatim port of ``timeline/app/ingest/gitrepo.py``. Only the package
imports changed (``app.*`` → ``neobrain.*``); repo discovery itself lives in
``config.discover_repos`` (NEOBRAIN_WORKSPACE driven).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Iterable

from .. import config
from ..models import Event, new

REC = "\x1e"
FIELD = "\x1f"
_NUMSTAT = re.compile(r"^(\d+|-)\t(\d+|-)\t(.+)$")


def _git(repo: Path, args: list[str]) -> str:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), *args],
            capture_output=True,
            text=True,
            timeout=60,
        )
        return proc.stdout
    except (subprocess.SubprocessError, OSError):
        return ""


def _severity(subject: str) -> str:
    s = subject.lower()
    if s.startswith(("revert", "rollback")):
        return "warning"
    if s.startswith("fix"):
        return "success"
    if s.startswith(("feat", "add")):
        return "notice"
    return "info"


def repository_name(repo: Path) -> str:
    # PORT-NOTE: config.WORKSPACE is Path | None in neobrain (it was always a
    # Path in the old app). discover_repos() never yields repos when it is None,
    # but guard anyway so the function is total.
    workspace = config.WORKSPACE
    if workspace is None:
        return repo.name
    try:
        rel = repo.relative_to(workspace)
    except ValueError:
        return repo.name
    if str(rel) in (".", ""):
        return "workspace-docs"
    return str(rel)


def iter_repo(repo: Path) -> list[Event]:
    fmt = f"{REC}%H{FIELD}%an{FIELD}%ae{FIELD}%cI{FIELD}%s{FIELD}%b"
    out = _git(
        repo,
        ["log", "--all", "--numstat", "--date=iso-strict", f"--pretty=format:{fmt}"],
    )
    events: list[Event] = []
    name = repository_name(repo)
    for chunk in out.split(REC):
        if not chunk.strip():
            continue
        lines = chunk.split("\n")
        first_numstat = next(
            (i for i, ln in enumerate(lines) if _NUMSTAT.match(ln)), len(lines)
        )
        preamble = "\n".join(lines[:first_numstat])
        parts = preamble.split(FIELD)
        if len(parts) < 6:
            continue
        commit, author, email, cdate, subject = parts[0], parts[1], parts[2], parts[3], parts[4]
        body = FIELD.join(parts[5:]).strip()

        files: list[dict] = []
        additions = deletions = 0
        for ln in lines[first_numstat:]:
            m = _NUMSTAT.match(ln)
            if not m:
                continue
            a, d, path = m.groups()
            adds = int(a) if a.isdigit() else 0
            dels = int(d) if d.isdigit() else 0
            additions += adds
            deletions += dels
            files.append({"path": path, "additions": adds, "deletions": dels})

        detail_bits = []
        if body:
            detail_bits.append(body)
        detail_bits.append(
            f"{len(files)} file(s), +{additions} -{deletions}"
        )
        detail = "\n\n".join(detail_bits)

        events.append(
            new(
                ts=cdate,
                source="git",
                lane="git",
                category="commit",
                actor=f"git:{author}",
                title=subject or commit[:10],
                source_ref=f"{name}:{commit}",
                detail=detail,
                severity=_severity(subject),
                refs={
                    "repo": name,
                    "commit": commit,
                    "author": author,
                    "email": email,
                    "files": files[:200],
                    "additions": additions,
                    "deletions": deletions,
                },
                raw={"body": body, "files": files},
            )
        )
    return events


def iter_events(repos: Iterable[Path] | None = None) -> list[Event]:
    repos = list(repos) if repos is not None else config.discover_repos()
    events: list[Event] = []
    for repo in repos:
        if (repo / ".git").is_dir():
            events.extend(iter_repo(repo))
    return events
