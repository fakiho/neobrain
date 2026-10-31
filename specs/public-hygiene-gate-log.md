# Run Log — Public-repo hygiene gate (`public-hygiene-gate`)

## 2026-10-31 — Slice decision
- User directive: the repo is **public**; no usernames, home paths, LAN IPs, key fragments, or private ops details may ever be pushed; add a pre-push verification rule. An audit found leaks in tracked files on main (docs/CUTOVER.md, six specs files, three src comment/docstring lines with a LAN IP) and in the public bodies of PRs #29/#31 (both since scrubbed and restored), plus two commit messages on main quoting the absolute path.
- Slice = AC-1 scanner + AC-2 scrub + AC-3 CI workflow. Rejected alternatives: regex-only local hook (hooks are not versioned; CI + orchestrator discipline cover it), full pytest in CI (scope creep; the deploy workflow already deploys, hygiene workflow gates privacy).
- Builder flagged + fixed a false-positive fleet: npm integrity blobs (`sha512-…-…`) match the bare gateway-key-prefix rule; resolved with token-start boundaries excluding `.`/`/`/`-` separators.
- Builder extended scope with user-approved necessity: same scrub applied to the two local-only ops spec drafts (untracked, kept out of the repo by the local exclude), and a behaviour-preserving type annotation fix in `config.py embed_api_key()` to clear a pre-existing pyright error blocking the 0-errors gate (identical values and order).
- Orchestrator own-artifact scrub: the two spec artifacts of the nightly-fixes slice + this slice's brief/log are repo-relative and literal-free (the brief names marker classes generically — a scanner must never flag its own spec).

## Gate results
- RED (qa AUTHOR): `tests/test_public_hygiene.py` — 24 tests, all failing for MISSING_BEHAVIOR (lazy-import pattern: scanner/workflow absent → runtime ImportError / pytest.fail inside tests), exit 1. qa ran an anti-vacuity harness in scratch: a reference implementation passed all 24; nine targeted mutations each broke exactly the intended test (redaction removed, empty-scan guard removed, label removed, binary skip removed, count missing, placeholder rule removed, `continue-on-error`, Python 3.11, `pull_request` removed).
- Orchestrator RED re-check: exit 1, `24 failed in 0.16s` — VALID RED.
- GREEN (builder): scanner (`scripts/check_public_hygiene.py`) → `scanned 121 git-tracked files, 0 marker(s) found`, exit 0; hygiene tests 24 passed exit 0; full suite 461 passed exit 0; pyright (with venv pythonpath) 0 errors on the four touched src files; deploy workflow untouched.
- GREEN gate (orchestrator-run, on the slice branch off main): scanner exit 0 (121 files, 0 markers); `tests/test_public_hygiene.py` 24 passed; full suite `451 passed` (427 on main + 24), exit 0.
- VALIDATE + REVIEW: (appended below after runs)

## History remediation (user decision pending)
- main's history contains the leaks in commit TREES (docs/specs/src) and in two commit MESSAGES. Trees are fixed going forward by this slice; messages and old trees are only removable by rewriting main (force-push — denied to the orchestrator; user's call). Also pending user decision: deleting the stale remote branches that carry older leak-bearing trees.
