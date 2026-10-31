# Run Log — Counterfactual Dream-Sandbox (`dream-sandbox`)

## 2026-10-29 — RECON & Slice 1 Decision

### Premise & Survival Rationale
The user granted full autonomy to choose and build what best helps the agent survive and improve.
A mind that only summarizes text during its nightly sleep (`light` → `rem` → `deep`) is a diary, not an empirical learner: unverified guesses, open architectural questions, and unconfirmed assumptions stay untested until a human prompts for them.

### What Was Reconnoitered Locally
- Repository: `~/neobrain` (322 passing tests in `.venv/bin/python -m pytest -q --tb=short`).
- `src/neobrain/dreams.py` runs `light`, `rem`, `deep`, and weekly `reflect`.
- `src/neobrain/life.py` invokes `dreams.run` during `_rest` at `life_dream_hour` and tracks once-per-day markers in the `events` table (`source='life'`).
- `src/neobrain/config.py` and `src/neobrain/settings_store.py` expose ritual toggles (`dreams_enabled`, `soul_reflect_enabled`, `consolidate_enabled`, `act_enabled`).
- `src/neobrain/cli.py` and `src/neobrain/api.py` expose CLI subcommands and Observatory HTTP endpoints.

### Slice 1 Decision
Build **Counterfactual Dream-Sandbox (`neobrain.sandbox`)** integrated into `neobrain`:
1. **Hypothesis & Experiment Generation**: Pulls open loops (`type='open'`) and recent decisions/lessons/observations from the mind (`mind.wake_up` / `mind.recall`), asks the LLM (`json_mode=True` with tier fallback `strong` → `cheap`) to formulate a concrete, falsifiable hypothesis (`question`, `hypothesis`, `prior_atom_id`, `hub`, `code`) as a self-contained Python 3 script that prints a JSON result (`{"verdict": "confirmed|refuted|inconclusive", "finding": "...", "metrics": {...}}`).
2. **Hermetic Isolated Execution**:
   - Static AST safety gate (`validate_code`) before execution: rejects imports of network/process/system-mutating modules (`socket`, `urllib`, `http`, `requests`, `httpx`, `subprocess`, `shutil`, `ctypes`, `multiprocessing`, `signal`, `pty`, `fcntl`, `resource`) and dangerous builtins/calls (`eval`, `exec`, `open` outside cwd, `os.system`, `os.popen`, `os.kill`, `os.remove`, `os.unlink`, `os.rmdir`, `os.chmod`, `os.chown`).
   - Runs in a throwaway `tempfile.TemporaryDirectory(prefix="neobrain-sandbox-")` with stripped environment (`HOME`, `TMPDIR` pointing to the throwaway dir, `PYTHONDONTWRITEBYTECODE=1`, no proxy/API keys), `subprocess.run([sys.executable, "-I", script_path], cwd=tmp, timeout=timeout_s, capture_output=True, text=True)`.
   - Never touches the real filesystem outside the throwaway directory, never reaches the network, and cleans up the directory on exit.
3. **Empirical Memory Promotion**:
   - Writes markdown + JSON experiment report to `<dream_root>/sandbox/<date>.md` and `<dream_root>/sandbox/<date>.json`.
   - When the experiment executes cleanly (`returncode == 0`) and yields a `confirmed` or `refuted` verdict with a non-empty `finding`, stores a durable `lesson` atom via `mind.remember(..., atype="lesson", source=f"memory/dreaming/sandbox/{date}.md", session_id="sandbox", dedupe=True)` and wires a `derived-from` link to `prior_atom_id` if present and valid.
   - Emits `life: dream sandbox` event with full status, verdict, runtime_ms, and stored lesson id.
4. **Life Loop, Config, CLI & API Wiring**:
   - `config.Settings`: `sandbox_enabled: str = "1"`, `sandbox_timeout_seconds: int = 10`.
   - `settings_store.REGISTRY`: `sandbox_enabled` (bool in `features`) and `sandbox_timeout_seconds` (int in `schedule`, min 1, max 60).
   - `life.LifeLoop._rest`: runs `sandbox.run(conn, self.runtime(), now=now)` during `life_dream_hour` when `sandbox_enabled` is truthy and `life: dream sandbox` has not run today.
   - `cli.py`: `neobrain sandbox [--replay YYYY-MM-DD]` subcommand.
   - `api.py`: `GET /api/mind/sandbox` returning recent sandbox experiment events/artifacts.

### Rejected Alternatives
- **Docker/container-per-night**: Rejected because spinning up Docker containers from inside the user systemd service adds daemon socket coupling and heavy image overhead when Python `-I` + AST capability gate + throwaway `TemporaryDirectory` provides fast, zero-dependency hermetic execution for algorithmic/benchmarking/counterfactual verification.
- **Standalone daemon**: Rejected per workspace rule ("single control-plane rule: lives in `neobrain`, zero new daemons, cadence in code + DB").

## 2026-10-29 — Slice 1 Gate Results & Review
- **RED Gate (`qa` AUTHOR, `ses_e7926e68dffedVDSkOwXBVegFy`)**: `tests/test_sandbox.py` authored (65 test cases across AC-1..AC-4). Command: `~/neobrain/.venv/bin/python -m pytest ~/neobrain/tests/test_sandbox.py -q --tb=short` → Exit code `1` (`65 failed`, `MISSING_BEHAVIOR` / `ASSERTION_MISMATCH`).
- **BUILD (`builder`, `ses_e792470bbffed4f18EhpUy5BKG`)**: Implemented `src/neobrain/sandbox.py` and wired `src/neobrain/config.py`, `src/neobrain/settings_store.py`, `src/neobrain/life.py`, `src/neobrain/cli.py`, and `src/neobrain/api.py`.
- **GREEN Gate**:
  - Test integrity: `tests/test_sandbox.py` untouched by builder.
  - Bytecode/syntax check (`py_compile`): Exit code `0`.
  - Full test suite (`~/neobrain/.venv/bin/python -m pytest -q --tb=short`): Exit code `0` (`387 passed, 1 warning in 45.77s`).
- **VERIFY (`qa` VALIDATE, `ses_e7919f81fffeFOLfkn2s8WqEQ5`)**: All 4 ACs (including E2E CLI + FastAPI TestClient verification) validated with exit code `0`.
- **REVIEW (`reviewer` TASK-REVIEW, `ses_e7915c13affeX2MnUx3gI0LMAy`)**: `VERDICT: PASS` (zero blockers; 1 non-blocking hardening note on `importlib`/`getattr` for future iterations).
- **FINAL AUDIT (`reviewer` FINAL-RECONCILIATION, `ses_e79138692ffeCLvkum2TjQaiVA`)**: `VERDICT: PASS`. Cross-checked brief AC blocks against log decisions and independently executed all AC verification methods (`65 passed` in `tests/test_sandbox.py`, `387 passed` in full suite, exit code `0`).

## 2026-10-29 — Slice 2 Decision: Live ROI Scorecard, No-Op Guard & AST Hardening

### Rationale
Following user review of Slice 1 and ROI discussion:
1. We need a built-in way to monitor the Sandbox's real-world ROI over 7 days (`sandbox.roi_report`, `GET /api/mind/sandbox/roi`, `neobrain sandbox --roi [--days 7]`) using the existing OpenCode plugin's automatic `served` (`m_rank`) and `used`/`useful`/`noise`/`unused` (`m_feedback`) signals on sandbox-authored lessons (`origin_session_id='sandbox'` or `source LIKE 'memory/dreaming/sandbox/%'`).
2. **Explicit user constraint**: Never auto-remove or auto-disable anything without the user's explicit instruction. The ROI scorecard is purely observational; the sandbox continues running until the user says otherwise.
3. **Anti-toy-experiment guard**: Prevent zero-ROI filler lessons when there is nothing real to test. If the store has neither candidate memories nor a day note, or if the LLM returns `{"skip": true, "reason": "..."}` when no falsifiable technical question exists, `sandbox.run` records `status="skipped"` (`reason="no-testable-hypothesis"`) and emits the nightly marker without running a dummy script or storing a lesson.
4. **AST Hardening**: Close the reviewer's note from Slice 1 by adding `importlib` and `builtins` to `FORBIDDEN_IMPORTS` and `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, `open` to `FORBIDDEN_BUILTINS`.

## 2026-10-29 — Slice 2 Gate Results
- **RED Gate (`qa` AUTHOR, `ses_e7902a379ffehs2E2pmkk2bYg8`)**: Added AC-5 and AC-6 tests to `tests/test_sandbox.py` (82 total tests). Command: `~/neobrain/.venv/bin/python -m pytest ~/neobrain/tests/test_sandbox.py -q --tb=short` → Exit code `1` (`17 failed, 65 passed`, `MISSING_BEHAVIOR` / `ASSERTION_MISMATCH`).
- **BUILD (`builder`, `ses_e78fe6cbdffeEDE2EqI5dkEMoO`)**: Implemented AC-5 and AC-6 in `src/neobrain/sandbox.py`, `src/neobrain/cli.py`, `src/neobrain/api.py`, and fixed hermetic override preservation in `src/neobrain/config.py`.
- **GREEN Gate**:
  - Test integrity: `tests/test_sandbox.py` untouched by builder.
  - Bytecode/syntax check (`py_compile`): Exit code `0`.
  - Full test suite (`~/neobrain/.venv/bin/python -m pytest -q --tb=short`): Exit code `0` (`404 passed, 1 warning in 43.30s`).
- **VERIFY (`qa` VALIDATE, `ses_e78d5f083ffexqnACD0N3IwWyB`)**: All 6 ACs (`AC-1`..`AC-6`) validated with exit code `0`.
- **REVIEW & FINAL AUDIT (`reviewer` TASK-REVIEW + FINAL-RECONCILIATION, `ses_e78cf354affeMV9oxHqlHqOzvJ`)**: `VERDICT: PASS` across all 6 ACs (`82 passed` in `tests/test_sandbox.py`, `404 passed` in full suite, exit code `0`; verified zero auto-disable/auto-removal).





