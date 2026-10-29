# Dream-Sandbox: nightly counterfactual experiments with ROI scorecard

## What
`neobrain` gains a **Counterfactual Dream-Sandbox** (`neobrain.sandbox`). Each night at the dream hour, the mind picks one open question or architectural assumption from its memory graph, has the LLM synthesize a small falsifiable Python experiment, runs it in a hermetic throwaway directory under a static AST safety gate, and — when the experiment confirms or refutes the hypothesis — promotes the finding into memory as a `lesson` atom linked `derived-from` the seed memory. A built-in ROI scorecard tracks whether those lessons are actually used by the live OpenCode plugin. A second commit in this branch adds an LLM-based memory-type classifier ("System One", `cloudflare/clef-flash`) that was completed alongside this branch.

## Why
Until now, neoBrain's nightly dreams only *summarized* notes — unverified guesses and open architectural questions stayed untested until a human prompted for them. The sandbox turns those open loops into cheap, empirical nighttime R&D (~one LLM call + ≤10 s CPU per night), so assumptions become tested facts before they are relied on in real tasks.

## Changes
- **`src/neobrain/sandbox.py` (new)** — the engine:
  - `validate_code()` — deterministic AST gate rejecting network/process/system-mutating imports (`socket`, `urllib`, `subprocess`, `ctypes`, … plus `importlib`/`builtins`), dangerous builtins (`eval`, `exec`, `open`, `getattr`, …), dunder access, and `os.*` mutations.
  - `execute_experiment()` — runs the experiment as `python3 -I experiment.py` in a `TemporaryDirectory` with a scrubbed environment and strict timeout; always cleans up.
  - `run()` — nightly pass: gathers candidates (`mind.wake_up` + day note), prompts the LLM (strong → cheap fallback, JSON mode), executes, writes `memory/dreaming/sandbox/<date>.{md,json}`, promotes confirmed/refuted findings as `lesson` atoms (`session_id="sandbox"`). Anti-toy guard: if there is nothing real to test (or the LLM returns `{"skip": true}`), the night is marked `skipped` with no experiment and no lesson.
  - `roi_report()` — windowed scorecard over the sandbox lessons: runs ok/skipped/failed, verdicts, lessons created, plus plugin-exposure signals (`served`, `used`, `useful`, `noise`, `unused`) from `m_rank`/`m_feedback`.
- **`src/neobrain/life.py`** — `LifeLoop._rest` invokes `sandbox.run` once per night at `life_dream_hour` (once-per-day guard via the `life: dream sandbox` event).
- **`src/neobrain/cli.py`** — `neobrain sandbox [--replay YYYY-MM-DD] [--roi] [--days N]`.
- **`src/neobrain/api.py`** — `GET /api/mind/sandbox?limit=30` (recent runs) and `GET /api/mind/sandbox/roi?days=7` (scorecard).
- **`src/neobrain/config.py` / `src/neobrain/settings_store.py`** — `sandbox_enabled` and `sandbox_timeout_seconds` (Observatory Settings tab), plus a fix preserving `EMBED_ENABLED=False` when no DB override exists (keeps tests hermetic).
- **`tests/test_sandbox.py` (new)** — 82 tests covering the AST gate, the isolated runner, the nightly pass + memory promotion, the life-loop/CLI/API wiring, the skip guard, and the ROI scorecard.
- **`specs/dream-sandbox-brief.md`, `specs/dream-sandbox-log.md`** — brief (6 ACs with verification methods) and the append-only run log.

Second commit (`feat(memory): classify memory types via System One decision model`): `runtime.py` gains a `/systemone` client (`cloudflare/clef-flash`) that classifies memory text into `lesson/decision/preference/observation`; `mind.remember` uses it for untyped remembers and day-note promotion with the deterministic regex fallback when the gateway is unavailable; `tests/test_decision_memory_type.py` covers it.

## How to test
```bash
cd /home/sparo/neobrain
.venv/bin/python -m pytest -q --tb=short          # full suite — expect: 424 passed
.venv/bin/python -m pytest tests/test_sandbox.py -q  # sandbox only — expect: 82 passed
neobrain sandbox --roi --days 7                    # print the ROI scorecard (JSON)
```
After merging, restart the daemon (`neobrain service restart`) so the nightly sandbox goes live, then check the 7-day scorecard with `neobrain sandbox --roi --days 7` or `GET http://<host>:9192/api/mind/sandbox/roi?days=7`.

## Evidence (AC → verification → result)
| AC | Verification | Result |
| --- | --- | --- |
| AC-1 AST safety gate | `pytest tests/test_sandbox.py -k test_ac1_` | 55 passed, exit 0 |
| AC-2 hermetic runner | `pytest tests/test_sandbox.py -k test_ac2_` | 4 passed, exit 0 |
| AC-3 nightly pass + lesson promotion | `pytest tests/test_sandbox.py -k test_ac3_` | 3 passed, exit 0 |
| AC-4 life-loop / settings / CLI / API wiring | `pytest tests/test_sandbox.py -k test_ac4_` (incl. E2E CLI + TestClient) | 3 passed, exit 0 |
| AC-5 AST hardening + skip guard | `pytest tests/test_sandbox.py -k test_ac5_` | 14 passed, exit 0 |
| AC-6 ROI scorecard (CLI + API) | `pytest tests/test_sandbox.py -k test_ac6_` (incl. `cli.main(["sandbox","--roi","--days","7"])` and `GET /api/mind/sandbox/roi?days=7`) | 3 passed, exit 0 |
| Full suite | `.venv/bin/python -m pytest -q --tb=short` | 424 passed, exit 0 |

## Gates & review
- **RED**: `qa` authored 82 tests before implementation — 65 failed (slice 1), then 17 failed (slice 2), exit 1 — valid `MISSING_BEHAVIOR`.
- **GREEN**: test integrity clean (builder never touched tests), `py_compile` exit 0, full suite 387 → 404 passed, exit 0.
- **VALIDATE**: `qa` re-ran every AC's verification method incl. E2E CLI/API — all exit 0.
- **REVIEW + FINAL-RECONCILIATION**: `reviewer` verdict **PASS** across AC-1…AC-6; confirmed the ROI report is read-only and nothing auto-disables or auto-removes the sandbox.

## Artifacts
- Brief: `specs/dream-sandbox-brief.md`
- Run log: `specs/dream-sandbox-log.md`
