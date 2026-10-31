# Fix: sandbox nightly runs — deterministic spec parsing + honest ROI lesson counts

## What
The Dream-Sandbox ran its first two live nights (Oct 30, Oct 31, 02:00) and both experiments were rejected — the mind produced zero findings. This PR fixes the two defects that caused it: the LLM reply parser now picks the spec deterministically (instead of the last JSON object in the reply, which let a model-echoed "sample result" shadow the real spec), and the ROI scorecard now counts only real promoted lessons (not the nightly report files that the dream-ingest stores as `dream` atoms).

## Why
Nightly runs ended `status: "rejected"` with `error: "empty code"` and empty question/hypothesis in both reports. Root cause: the model prints the experiment spec JSON followed by an example output JSON; the parser (`_parse_last_json_object`, designed for experiment *stdout* where the last verdict wins) picked the sample, leaving `code` empty, so the AST safety gate rejected the run. Separately, the ROI scorecard reported `lessons_created: 2` — but both were empty report-file artifacts, not lessons, which would have corrupted the 7-day keep/kill scorecard.

## Changes
- `src/neobrain/sandbox.py`:
  - New `_iter_json_objects()` — every parseable JSON object in the reply, in document order, fence-tolerant.
  - New `_parse_spec_object()` — the object carrying a `"code"` string wins (first if several); otherwise the first non-empty object (protects `{"skip": true}` specs from being shadowed); `None` keeps the existing error path.
  - `_build_prompt()` — explicit reply contract: exactly ONE JSON object (the spec); never echo example/sample output.
  - `roi_report()` — lesson counts filter `AND type = 'lesson'` across all provenance branches.
- `tests/test_sandbox.py` — 10 new tests (purely additive; all 82 pre-existing sandbox tests untouched and green).

## How to test
```
.venv/bin/python -m pytest -q --tb=short
```
→ `437 passed, 1 warning`, exit 0. (92 sandbox tests: 82 pre-existing + 10 new.)
Typecheck: `npx pyright src/neobrain/sandbox.py` → `0 errors, 0 warnings`, exit 0.
Live behaviour: after the daemon restarts, tonight's 02:00 run should produce `status: "ok"` (or a legitimate `skipped`) instead of `rejected`; ROI (`neobrain sandbox --roi --days 7`) must show only real lessons.

## Evidence
| AC | Verification | Result |
|----|--------------|--------|
| AC-1 spec extraction | 8 named tests (trailing/leading sample, fenced pretty-printed, first-of-many, first-dict fallback, skip protection, prompt bans echo) | all pass; plus live-gateway E2E on a temp store: poisoned spec+sample reply → spec selected, experiment executed, `status ok`, lesson promoted |
| AC-2 ROI lesson filter | 2 named tests (dream artifact excluded; filter across source/session branches) | all pass; temp-store ROI shows `lessons_created == 1` with a dream artifact present |
| Full suite | exact verify command | `437 passed, 1 warning`, exit 0 |

## Gates & review
- RED: exit 1, 9 valid failures (MISSING_BEHAVIOR / ASSERTION_MISMATCH) — tests authored from the brief before implementation.
- GREEN: full suite 437 passed exit 0; pyright on the changed file 0 errors; test diff purely additive.
- VALIDATE: both ACs met with pasted evidence, including a real gateway call against an isolated temp store (production DB untouched).
- Reviewer (TASK-REVIEW): **PASS**, no blockers; 4 non-blocking WARNs (all pre-existing residuals or wording fixes, logged).

## Artifacts
- Brief: `specs/sandbox-nightly-fixes-brief.md`
- Run log: `specs/sandbox-nightly-fixes-log.md`
