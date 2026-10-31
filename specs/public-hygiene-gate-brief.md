# Brief — Public-repo hygiene gate (`public-hygiene-gate`)

## Context
The repo (`fakiho/neobrain`) is **public**. An audit on 2026-10-31 found private markers pushed in file content across history: the host owner's absolute home paths in `docs/CUTOVER.md`, six `specs/*` files and three `src/` comment/docstring lines, plus a LAN IP (`<rfc1918-ip>:11434`) in `src/neobrain/embeddings.py`'s docstring. This slice (1) scrubs every current occurrence from tracked files and (2) adds a committed hygiene scanner + CI workflow so every future push is verified before it lands. The scanner is also the orchestrator's mandatory pre-push gate.

## AC-1: Hygiene scanner script
- **What it does**: `scripts/check_public_hygiene.py` scans every **git-tracked** file (`git ls-files -z`, so local-only excludes are never scanned or committed) for private markers: absolute `/home/<name>/` paths (allowing generic placeholders `x`, `user`, `username`, `you`), the host owner's 5-letter login name as a word, RFC1918 LAN addresses (dotted quads starting in the private /16, /8, or /12 ranges), the owner's gmail address prefix, and common gateway key prefixes (`sk-`, `oc` + 20+ base62 chars, `st_`) as token matches. Binary files skipped. Exit `0` when clean; exit `1` printing one `file:line` per match with the match **redacted** (marker type + at most 6 leading chars of the match text, never the full line). **Empty-scan guard**: if zero tracked files are found, exit `1` with an error — a scan of nothing is a failed scan, never a pass.
- **Verification method**: Automated tests in `tests/test_public_hygiene.py`: unit tests drive the scan function over temp trees (planted leaks of each marker class → each detected with correct line number; placeholder `/home/x/` and version-number-like `10.5` text → not flagged; redaction — output never contains a full planted secret; empty-tree input → error exit). Tree test: run the scanner over the real repo via `git ls-files` and assert it exits 0 with a non-empty scanned-file count.

## AC-2: Repo scrubbed of current leaks
- **What it does**: No tracked file contains any marker AC-1 flags. Specifically: `docs/CUTOVER.md` paths become generic (`~/timeline/data/timeline.db`, `~/backups/...`, `~/neobrain/...` or `the repo checkout`); the `src/neobrain/config.py` / `dreams.py` comments describing the *old* hardcoded path say so without the literal; `src/neobrain/embeddings.py` docstring says the LAN Ollama endpoint without the numeric IP; `specs/dream-sandbox-*` and `specs/decision-memory-type-*` use repo-relative commands. Behaviour of any code is unchanged — comments/docs only (plus allowed prose rewrites in specs).
- **Verification method**: The scanner itself (AC-1) exits 0 on the repo; full test suite unchanged-green.

## AC-3: CI gate
- **What it does**: New workflow `.github/workflows/hygiene.yml` — on `push` (all branches) and `pull_request`: checkout, setup Python 3.12, run `python scripts/check_public_hygiene.py`. Must fail the workflow on any match. (Deploy workflow untouched.)
- **Verification method**: Automated: the workflow YAML parses (PyYAML) and its step runs the scanner command; observable: the check appears and passes on the PR after push.

## File-Touch Manifest
- Production: `scripts/check_public_hygiene.py` (new), `.github/workflows/hygiene.yml` (new), `docs/CUTOVER.md`, `src/neobrain/config.py`, `src/neobrain/dreams.py`, `src/neobrain/embeddings.py` (comment/docstring lines only), `specs/dream-sandbox-brief.md`, `specs/dream-sandbox-log.md`, `specs/dream-sandbox-pr.md`, `specs/decision-memory-type-brief.md`, `specs/decision-memory-type-log.md`, `specs/decision-memory-type-pr.md`
- Tests: `tests/test_public_hygiene.py` (new)

## Exact Verify Command
```bash
.venv/bin/python -m pytest -q --tb=short
.venv/bin/python scripts/check_public_hygiene.py
```
