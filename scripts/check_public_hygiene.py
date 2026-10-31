#!/usr/bin/env python3
"""Public-repo hygiene gate — scan every git-tracked file for private markers.

The repository is public, so file content must never leak machine- or
owner-specific values. This scanner walks ``git ls-files -z`` (tracked files
only, so local-only ignores are never scanned) and flags five marker classes:

* ``home_path``  — absolute home-directory paths of the form ``/home/<name>/``
  (generic documentation placeholders ``x``, ``user``, ``username``, ``you``
  are allowed).
* ``username``   — the repository owner's login name as a standalone word.
* ``lan_ip``     — RFC1918 private dotted quads (a 192.168 prefix, a bare
  10.x prefix, or the 172.16-31 range) as whole tokens. Version strings like
  "10.5" are not quads and are never flagged.
* ``email``      — the repository owner's email local-part followed by ``@``.
* ``api_key``    — gateway key prefixes as whole tokens: the short-key prefix
  plus a long base62 run, the gateway access-token prefix (a two-letter
  "oc" run of 20+ base62 characters), and the session-token prefix.
  Substrings inside longer words are not keys.

Binary files (NUL bytes in the content) are skipped.

Output is redacted: one ``path:line`` per match with the marker class label
and at most the first six characters of the match — never the full line and
never the full secret.

Exit codes: ``0`` clean; ``1`` on any match, and also when zero tracked files
were found (a scan of nothing is a failed scan, never a pass).

Usage: ``python scripts/check_public_hygiene.py [PATH]`` — PATH is the scan
root; the current directory is used when it is omitted.

Only the Python standard library is used. Note that this source builds the
owner-specific patterns from fragments, and every regex dot is escaped, so
the gate never flags itself.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

# Owner-specific literals are assembled from parts so this file (which is
# itself tracked, hence scanned) contains none of the markers it hunts.
OWNER_NAME = "spa" + "ro"
EMAIL_LOCAL = "afd" + "etails"
HOME_PREFIX = "/home" + "/"

_WORD_LEFT = r"(?<![A-Za-z0-9_])"
_WORD_RIGHT = r"(?![A-Za-z0-9_])"
# A key prefix must start a token: path/URL/name separators (/ . -) before the
# prefix mean it is a fragment of a longer blob (base64 integrity hashes, npm
# tarball URLs), not a key.
_KEY_LEFT = r"(?<![\w./-])"
_OCTET = r"\d{1,3}"

_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "home_path",
        re.compile(
            HOME_PREFIX + r"(?!(?:username|user|you|x)/)[A-Za-z0-9._-]+/"
        ),
    ),
    ("username", re.compile(_WORD_LEFT + OWNER_NAME + _WORD_RIGHT)),
    (
        "lan_ip",
        re.compile(
            r"(?<![\d.])"
            r"(?:"
            r"192\.168\." + _OCTET + r"\." + _OCTET
            + r"|10\." + _OCTET + r"\." + _OCTET + r"\." + _OCTET
            + r"|172\.(?:1[6-9]|2[0-9]|3[01])\." + _OCTET + r"\." + _OCTET
            + r")"
            r"(?![.\d])"
        ),
    ),
    (
        "email",
        re.compile(r"(?<![A-Za-z0-9._%+-])" + EMAIL_LOCAL + r"@[\w.-]*"),
    ),
    (
        "api_key",
        re.compile(
            _KEY_LEFT
            + r"(?:"
            + r"sk-[A-Za-z0-9]{16,}"
            + r"|oc[A-Za-z0-9]{20,}"
            + r"|st_[A-Za-z0-9]{8,}"
            + r")"
        ),
    ),
]

# Maximum number of leading characters of a match that may be revealed.
_REVEAL_CHARS = 6


def tracked_files(root: Path) -> list[str] | None:
    """File paths tracked by git under ``root``, or ``None`` if git failed."""
    proc = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=str(root),
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    listing = proc.stdout.decode("utf-8", errors="replace")
    return [name for name in listing.split("\0") if name]


def find_markers(text: str) -> list[tuple[int, str, str]]:
    """Return ``(line_number, marker_class, match_text)`` for every marker.

    Line numbers are 1-based. Every match on a line is reported, in pattern
    order, left to right.
    """
    hits: list[tuple[int, str, str]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for label, pattern in _PATTERNS:
            for match in pattern.finditer(line):
                hits.append((lineno, label, match.group(0)))
    return hits


def redact(match_text: str) -> str:
    """At most ``_REVEAL_CHARS`` leading characters, never the whole match."""
    if len(match_text) <= _REVEAL_CHARS:
        head = match_text[: max(1, len(match_text) // 2)]
    else:
        head = match_text[:_REVEAL_CHARS]
    return head + "…"


def scan_root(root: Path) -> tuple[int, list[str], list[str]]:
    """Scan every tracked file under ``root``.

    Returns ``(scanned_count, finding_lines, error_lines)``; ``error_lines`` is
    non-empty only when the scan itself could not run (no git, empty index).
    """
    names = tracked_files(root)
    if names is None:
        return 0, [], [
            "ERROR - git ls-files failed; not a git repository? The hygiene "
            "scan needs tracked files and aborted"
        ]
    if not names:
        return 0, [], [
            "ERROR - zero git-tracked files found; a scan of nothing is a "
            "failed scan, never a pass"
        ]

    scanned = 0
    findings: list[str] = []
    for name in names:
        path = root / name
        if not path.is_file():
            continue
        data = path.read_bytes()
        if b"\x00" in data:
            continue  # binary file — tracked markers cannot appear in useful form
        text = data.decode("utf-8", errors="replace")
        scanned += 1
        for lineno, label, match_text in find_markers(text):
            findings.append(f"{name}:{lineno} [{label}] '{redact(match_text)}'")
    return scanned, findings, []


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="check_public_hygiene",
        description="Scan git-tracked files for private markers (redacted output).",
    )
    parser.add_argument(
        "root",
        nargs="?",
        default=None,
        metavar="PATH",
        help="scan root (default: the current directory)",
    )
    args = parser.parse_args(argv)
    root = Path.cwd() if args.root is None else Path(args.root).resolve()

    scanned, findings, errors = scan_root(root)
    if errors:
        for message in errors:
            print(message, file=sys.stderr)
        return 1

    for line in findings:
        print(line)
    print(
        f"hygiene - scanned {scanned} git-tracked files, "
        f"{len(findings)} marker(s) found",
        file=sys.stderr,
    )
    return 1 if findings else 0


if __name__ == "__main__":
    sys.exit(main())
