# chore: public-repo hygiene — leak scrub + pre-push hygiene gate

## What
The repository is public. An internal audit found that private markers had been committed in file content: absolute home paths of the machine owner in `docs/CUTOVER.md`, six `specs/` artifacts and three `src/` comment/docstring lines, plus a LAN IP in `src/neobrain/embeddings.py`'s docstring. This PR scrubs every occurrence from the tracked files and adds a committed scanner plus a CI workflow that fails any push or PR containing such markers.

## Why
Public repos must not disclose hostnames, usernames, home-directory layouts, internal network addresses, email addresses, or credential fragments — whether in code, docs, specs, commit messages, or PR descriptions. The scanner makes that a hard, automated gate instead of a manual aspiration.

## Changes
- `scripts/check_public_hygiene.py` (new) — stdlib-only CLI: scans every git-tracked file (local-only excludes are never scanned) for absolute `/home/<name>/` paths (generic placeholders allowed), the owner's login name as a word, RFC1918 LAN addresses, the owner's email prefix, and gateway key prefixes (with token boundaries so npm integrity blobs don't false-positive). Binary files skipped; match output redacted (marker type + ≤6 leading chars, never the full line); **a scan of zero files is an error, never a pass**; summary prints the scanned-file count.
- `.github/workflows/hygiene.yml` (new) — runs the scanner on every `push` and `pull_request`; no failure tolerance.
- Scrubbed (comments/docs/prose only, zero behaviour change): `docs/CUTOVER.md`, `src/neobrain/config.py`, `src/neobrain/dreams.py`, `src/neobrain/embeddings.py`, `specs/dream-sandbox-*.md`, `specs/decision-memory-type-*.md`.
- `tests/test_public_hygiene.py` (new) — 24 tests: per-marker detection with exact line numbers, placeholder/version false-positive guards, redaction bounds, binary skip, untracked-exclude skip, empty-scan guard, repo-clean tree check, and workflow YAML contract (triggers, no failure tolerance, Python 3.12).

## How to test
```bash
python scripts/check_public_hygiene.py        # expect: "scanned N git-tracked files, 0 marker(s) found", exit 0
.venv/bin/python -m pytest -q --tb=short      # expect: 451 passed, exit 0
```

## Evidence
| AC | Verification | Result |
|----|--------------|--------|
| AC-1 scanner | 24 automated tests (planted leaks per marker class, line numbers, redaction, guards) | 24 passed, exit 0 |
| AC-2 repo scrubbed | the scanner over the real repo | `0 marker(s) found` over 121 tracked files, exit 0 |
| AC-3 CI gate | workflow-YAML contract tests; observable check after push | workflow parses, runs the scanner, triggers on push + pull_request, no failure tolerance |

## Gates & review
- RED: 24 failing tests for missing behaviour (exit 1), including an anti-vacuity mutation harness run by the test author.
- GREEN: full suite 451 passed, exit 0; scanner clean; pyright 0 errors on touched files; test diff confined to the new test file.
- Reviewer verdict: appended to the run log (`specs/public-hygiene-gate-log.md`).

## Artifacts
- Brief: `specs/public-hygiene-gate-brief.md`
- Run log: `specs/public-hygiene-gate-log.md`
