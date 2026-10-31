# Brief — Sandbox nightly-run fixes: spec parser & ROI lesson filter (`sandbox-nightly-fixes`)

## Context (live finding, 2026-10-30 + 2026-10-31 nights)
Both nightly runs ended `status="rejected"` with report files written empty (no question/hypothesis). Root cause: the LLM echoes a sample experiment-result JSON **after** the spec JSON; `run()` picks the spec with `_parse_last_json_object`, which prefers the **last** valid `{verdict, finding}` object — so the sample shadows the real spec, `code` comes back empty, and the AST gate rejects "empty code". Secondary bug: `roi_report` counts the empty sandbox report files (ingested nightly as `type="dream"` atoms) as `lessons_created`, inflating ROI.

## AC-1: Deterministic spec extraction (no sample-JSON shadowing)
- **What it does**: In `sandbox.run`, the LLM reply must be parsed so the **spec contract wins**: parse all JSON objects in the reply; the spec is the object that has a `"code"` key (string). If multiple objects have `code`, take the first. If none has `code`, fall back to the **first** parseable dict (so a `{"skip": true}` spec is never shadowed by a trailing sample object). Additionally `_build_prompt`'s instructions must state: return exactly ONE JSON object (the spec) and never echo example or sample output JSON.
- **Verification method**: Automated tests in `tests/test_sandbox.py`: a reply where the spec is followed by a trailing sample `{"verdict": "confirmed", "finding": "..."}` object still yields the spec (question/code extracted, experiment executed, status ok); and the prompt text forbids echoing sample output.

## AC-2: ROI counts only real promoted lessons
- **What it does**: `sandbox.roi_report`'s `lessons_created` and per-lesson list include only atoms with `type = 'lesson'` and `source LIKE 'memory/dreaming/sandbox/%'` (or `session_id='sandbox'`), excluding `type='dream'` artifacts that the nightly ingest creates from the report files.
- **Verification method**: Automated tests in `tests/test_sandbox.py`: seed a `type='dream'` atom with sandbox source + a real `type='lesson'` sandbox atom → `lessons_created == 1`, lessons list contains only the lesson atom.

## File-Touch Manifest
- Production: `src/neobrain/sandbox.py` only
- Tests: `tests/test_sandbox.py` only

## Exact Verify Command
```bash
~/neobrain/.venv/bin/python -m pytest -q --tb=short
```
