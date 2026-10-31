# Run PR — decision-model memory typing + dream-sandbox + dedicated gateway key

## What

neoBrain now classifies memories with a decision model instead of keyword regexes: when an
atom is written without an explicit type, or when the nightly dream pass reads a daily-note
section, the text is sent to `cloudflare/clef-flash` through the house LiteLLM gateway's
`/systemone` endpoint. The model answers `lesson` / `decision` / `preference` /
`observation`, and the answer decides whether the memory becomes curated (auto-injected into
future sessions) or stays a plain observation. If the gateway is down, unconfigured, or
errors, the old deterministic regex classifier takes over, so a memory write never fails
because of the network.

Alongside it, this branch carries the counterfactual dream-sandbox slice (nightly falsifiable
experiments with an ROI scorecard) and a small ops change that gives neoBrain its own gateway
credential on this host.

## Why

Most of what an agent learns arrives through tools that do not set a type, so it was stored as
`observation` — and observations are deliberately excluded from in-turn recall. Real lessons and
decisions sat in the store unusable, and the daily-note importer stranded them behind regexes that
only fire on specific keywords. An empirical check on 24 real atoms from the live store showed the
regexes missing 6 of 8 genuine lessons/decisions while the decision model recovered all 6 at
~0.35 s and ~$0.000009 per atom, with no false positives on true status logs.

The credential change answers a separate concern: neoBrain was spending under a shared gateway
key, so its traffic could not be attributed or budgeted on its own.

## Changes

**Decision-model memory typing**
- `src/neobrain/runtime.py` — new `/systemone` client: `decision_url()`, Pydantic v2 wire models
  (`DecisionQuestion`/`DecisionRequest`/`DecisionAnswer`/`DecisionResponse`), and
  `classify_memory_type()`, a single-attempt call with a 2 s timeout that returns `None` instead of
  raising on any failure. A `"decision"` model tier was added to `_resolve_model`.
- `src/neobrain/mind.py` — `remember()` classifies untyped/default memories and assigns the
  curated weight for the resulting type; `_promotion_kind()` delegates to the model and keeps
  `_regex_promotion_kind()` as the offline fallback; `_promote_daily_notes()` skips
  already-curated and very short sections before spending a call, and an in-memory cache prevents
  re-querying unchanged sections on every dream pass. A model verdict of `observation` is final —
  it is not second-guessed by keyword regexes.
- `src/neobrain/settings_store.py` — `llm_model_decision` registered in the Observatory settings
  (section *cognition*), editable at runtime; `Setting.type` and `get_public()` expose it.
- `src/neobrain/config.py` — `llm_model_decision` defaults to `cloudflare/clef-flash`.
- `tests/test_decision_memory_type.py` — 20 tests covering config/settings, the wire client and
  its fail-open behaviour, `remember()` typing and weighting, and day-note promotion, caching and
  fallback.

**Dream-sandbox (earlier slice on this branch)**
- `src/neobrain/sandbox.py` — AST-gated, `-I`-isolated experiment runner with a strict timeout;
  confirmed/refuted findings are promoted to the mind as `lesson` atoms.
- `src/neobrain/{api,cli,life,config}.py` — endpoints, CLI subcommands (`neobrain sandbox`,
  `--roi`) and the rest-phase hook.
- `tests/test_sandbox.py`.

**Ops (record kept privately, not in the repo)**
- neoBrain on this host now runs on its own dedicated gateway credential (per-machine attribution
  is deliberate). Details live in the private ops log only — key aliases, spend and per-device
  credentials are not published here.

## How to test

```bash
# from the repo root

# slice tests
.venv/bin/python -m pytest tests/test_decision_memory_type.py -q --tb=short
# expect: 20 passed

# full suite
.venv/bin/python -m pytest -q --tb=short
# expect: 424 passed

# live gateway check (requires the gateway on :4000 and a working key in .env)
.venv/bin/python -c 'from neobrain import runtime; print(runtime.classify_memory_type("Fixed SQLite WAL lock contention by opening per-worker connections after fork."))'
# expect: lesson
```

Offline behaviour: point `NEOBRAIN_LLM_BASE_URL` at a closed port and run the suite — the typing
tests still pass, because classification falls back to the regex rules and returns `None` fast
rather than retrying.

## Evidence

| AC | Verification | Result |
| --- | --- | --- |
| AC-1 config + settings | `pytest tests/test_decision_memory_type.py::test_ac1_decision_model_config_and_settings` | pass, exit 0 |
| AC-2 `/systemone` client, fail-open | 3 `test_ac2_*` tests (request shape, response variants, no-retry fail-open) | pass, exit 0 |
| AC-3 `remember()` typing + weights | 12 `test_ac3_*` tests incl. API path and explicit-type bypass | pass, exit 0 |
| AC-4 promotion, caching, fallback | 4 `test_ac4_*` tests | pass, exit 0 |
| AC-4 live end-to-end | `/systemone` on a temp store: untyped writes classified lesson/decision/preference/observation at weights 0.85/0.88/0.90/0.62; a semantic day-note promoted to `m_pd_*` with a `derived-from` edge; a status note full of incidental keywords not promoted | exit 0 |
| Full regression | `pytest -q` | 424 passed, exit 0 |

## Gates & review

- RED — 20 new tests failed for missing behaviour (`20 failed, 1 warning`), as intended.
- GREEN — test files untouched by the builder; `py_compile` clean; full suite `424 passed`.
- VALIDATE — QA ran the suite, every AC's tests, and a live `/systemone` E2E; all exit 0.
- Review — TASK-REVIEW: PASS. FINAL-RECONCILIATION: PASS, with the reviewer re-running the
  tests and the live E2E itself rather than trusting the earlier output.
- Ops slice — reviewer returned BLOCKED on two items its sandbox could not run (`.env` mode,
  a literal fresh-process dual call); both were executed by the orchestrator afterwards
  (mode `600`, owner-only; `classify: lesson | chat: ok`, exit 0) and the blockers closed.

## Artifacts

- `specs/decision-memory-type-brief.md`, `specs/decision-memory-type-log.md`
- `specs/dream-sandbox-brief.md`, `specs/dream-sandbox-log.md`
