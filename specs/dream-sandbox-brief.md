# Slice 1 Brief — Counterfactual Dream-Sandbox (`dream-sandbox`)

## Goal
Give `neobrain` an empirical nocturnal R&D engine (`neobrain.sandbox`). During the nightly rest window (or on demand via CLI/API), the mind selects an open question or recent architectural assumption from its memory graph, synthesizes a small, falsifiable Python experiment, executes it inside an AST-gated hermetic temporary directory under `-I` (isolated mode) with a strict timeout, writes the experiment report (`memory/dreaming/sandbox/<date>.md` and `.json`), and promotes empirically confirmed or refuted findings back into the mind as `lesson` atoms linked `derived-from` the seed memory.

## Acceptance Criteria

### AC-1: Static AST Safety Gate (`sandbox.validate_code`)
- **What it does**: `neobrain.sandbox.validate_code(code: str) -> tuple[bool, Optional[str]]` parses Python source with `ast.parse` and deterministically rejects:
  1. Syntax errors or empty code.
  2. Imports (`import X` or `from X import ...`) whose top-level module is in the forbidden set: `socket`, `urllib`, `http`, `requests`, `httpx`, `aiohttp`, `ftplib`, `smtplib`, `telnetlib`, `xmlrpc`, `subprocess`, `shutil`, `ctypes`, `multiprocessing`, `signal`, `pty`, `fcntl`, `resource`, `webbrowser`, `ensuredir`, `pip`, `venv`.
  3. Calls to forbidden builtins (`eval`, `exec`, `compile`, `__import__`, `input`, `breakpoint`) or dunder attribute access (any `ast.Attribute` where `attr.startswith("__")`).
  4. Calls to dangerous `os`/`pathlib` mutations (`os.system`, `os.popen`, `os.spawn*`, `os.exec*`, `os.fork`, `os.kill`, `os.killpg`, `os.remove`, `os.unlink`, `os.rmdir`, `os.removedirs`, `os.rename`, `os.replace`, `os.chmod`, `os.chown`, `os.chroot`, `os.symlink`, `os.link`).
  Safe standard-library computational modules (`math`, `statistics`, `time`, `json`, `re`, `collections`, `itertools`, `functools`, `hashlib`, `random`, `sqlite3`, `dataclasses`, `typing`, `decimal`, `fractions`, `heapq`, `bisect`, `array`, `struct`) pass validation and return `(True, None)`.
- **Verification method**: Automated unit tests in `tests/test_sandbox.py`.

### AC-2: Hermetic Execution Runner (`sandbox.execute_experiment`)
- **What it does**: `neobrain.sandbox.execute_experiment(code: str, *, timeout: float = 10.0) -> dict` first validates `code` with `validate_code`. If rejected, it returns `{"ok": False, "status": "rejected", "error": reason, "returncode": None, "stdout": "", "stderr": "", "duration_ms": 0, "result": None}` without spawning a process. If valid, it creates a throwaway `tempfile.TemporaryDirectory(prefix="neobrain-sandbox-")`, writes `experiment.py` inside it, runs `[sys.executable, "-I", "experiment.py"]` with `cwd=tmp_dir`, a scrubbed environment (`{"PATH": "/usr/bin:/bin", "HOME": tmp_dir, "TMPDIR": tmp_dir, "PYTHONDONTWRITEBYTECODE": "1", "PYTHONIOENCODING": "utf-8"}`), and `timeout=timeout`.
  - On timeout (`subprocess.TimeoutExpired`), returns `{"ok": False, "status": "timeout", "error": ..., ...}`.
  - On non-zero exit, returns `{"ok": False, "status": "failed", "returncode": rc, "stderr": ..., ...}`.
  - On exit 0, parses the last JSON object printed to `stdout` (tolerating leading log lines or markdown fences) expecting keys `verdict` (`"confirmed"` | `"refuted"` | `"inconclusive"`), `finding` (`str`), and optional `metrics` (`dict`). Returns `{"ok": True, "status": "ok", "returncode": 0, "stdout": ..., "stderr": ..., "duration_ms": ..., "result": parsed_dict}`.
  - The temporary directory is always removed before `execute_experiment` returns.
- **Verification method**: Automated tests in `tests/test_sandbox.py` covering valid execution, timeout enforcement, non-zero exit, rejected unsafe code, and verification that the temp directory is cleaned up.

### AC-3: End-to-End Nightly Sandbox Pass & Memory Promotion (`sandbox.run`)
- **What it does**: `neobrain.sandbox.run(conn: sqlite3.Connection, runtime: Any, *, now: Optional[datetime] = None) -> dict`:
  - Uses `_dream_date(now)` (same rule as `dreams.py`: before noon belongs to the previous day's date) and checks the once-per-day event marker `life: dream sandbox` (returns `{"status": "skipped", "reason": "already-ran-tonight", "date": date}` if already run on `now.date()`).
  - Gathers candidate material from `mind.wake_up(conn)` and the day note (`memory/<date>.md`), prompts `runtime.chat` (tier `"strong"` with fallback to `"cheap"`, `json_mode=True`) for a JSON specification:
    `{"question": "...", "hypothesis": "...", "hub": "...", "prior_atom_id": "<atom id or null>", "code": "<python3 script>"}`.
  - Runs `execute_experiment(code, timeout=config.settings.sandbox_timeout_seconds)`.
  - Writes `<dream_root>/sandbox/<date>.json` (full machine record: question, hypothesis, hub, prior_atom_id, execution status, duration_ms, result) and `<dream_root>/sandbox/<date>.md` (`# Dream Sandbox`, question, hypothesis, verdict, finding, metrics).
  - When execution succeeds (`ok=True`) and `result["verdict"] in ("confirmed", "refuted")` with a non-empty `result["finding"]`, stores a `lesson` atom via `mind.remember(conn, finding_text, atype="lesson", hubs=[hub] if hub else ["agent"], source=f"memory/dreaming/sandbox/{date}.md", session_id="sandbox", dedupe=True)`. If `prior_atom_id` exists in `m_atoms`, wires `mind.link(conn, new_atom_id, prior_atom_id, "derived-from", session_id="sandbox", explicit=False)`.
  - Emits event `title="life: dream sandbox"` (`source="life"`, `lane="notice"`, `category="life"`, `actor="agent:neobrain"`) with summary and details, and never raises even if the LLM or experiment fails.
- **Verification method**: Automated tests in `tests/test_sandbox.py` using `FakeRuntime` and real hermetic SQLite + subprocess execution.

### AC-4: Life Loop, Settings, CLI & API Integration
- **What it does**:
  - `config.Settings` adds `sandbox_enabled: str = "1"` and `sandbox_timeout_seconds: int = 10`.
  - `settings_store.REGISTRY` adds `Setting("sandbox_enabled", "Dream sandbox", "bool", "features", ...)` and `Setting("sandbox_timeout_seconds", "Sandbox timeout", "int", "schedule", ..., minimum=1, maximum=60, suffix="s")`.
  - `life.LifeLoop._rest` invokes `sandbox.run(conn, self.runtime(), now=now)` during `life_dream_hour` when `sandbox_enabled` is enabled and `life: dream sandbox` has not run today, appending `"sandbox"` to the returned `ran` list.
  - `cli.py` adds `neobrain sandbox [--replay YYYY-MM-DD]` subcommand calling `sandbox.run` (with `datetime(y, m, d, 22, 30)` when `--replay` is given) and printing the JSON result.
  - `api.py` adds `GET /api/mind/sandbox?limit=30` returning `{"count": len(items), "items": items}` from `sandbox.history(conn, limit=limit)` (listing recent `life: dream sandbox` events with parsed detail).
- **Verification method**:
  - Automated tests in `tests/test_sandbox.py` covering `LifeLoop.tick()` at `life_dream_hour`, `cli.main(["sandbox", ...])`, `GET /api/mind/sandbox`, and `settings_store.snapshot`.
  - E2E CLI & API verification command executing `neobrain sandbox` against a temporary store with a mocked/local runtime and querying the FastAPI TestClient.

### AC-5: AST Hardening & Anti-Toy-Experiment No-Op Guard (Slice 2)
- **What it does**:
  - `sandbox.validate_code` also rejects imports of `importlib` and `builtins` (in `FORBIDDEN_IMPORTS`) and calls/references to `getattr`, `setattr`, `delattr`, `globals`, `locals`, `vars`, and `open` (in `FORBIDDEN_BUILTINS`).
  - `sandbox.run` enforces an anti-toy-experiment no-op guard:
    1. `_build_prompt` instructs the model that if there is no concrete open question or falsifiable technical claim in the candidate memories or day note, it MUST return `{"skip": true, "reason": "no-testable-hypothesis"}` instead of inventing a trivial or toy test.
    2. When the LLM returns JSON with `"skip": true` (or `status == "skip"`), `sandbox.run` does NOT call `execute_experiment` and does NOT store a lesson; it emits the `life: dream sandbox` event with `status="skipped"`, `reason=spec.get("reason") or "no-testable-hypothesis"` and returns `{"status": "skipped", "reason": reason, "date": date, "atom_id": None}`.
- **Verification method**: Automated tests in `tests/test_sandbox.py` verifying `importlib`/`builtins`/`getattr`/`setattr`/`delattr`/`globals`/`locals`/`vars`/`open` rejection, prompt instruction for `skip: true`, and `sandbox.run` skipping execution & lesson creation when the LLM returns `{"skip": true, "reason": "no-testable-hypothesis"}`.

### AC-6: Live 7-Day ROI Scorecard (`sandbox.roi_report`, CLI `--roi`, API `/api/mind/sandbox/roi`) (Slice 2)
- **What it does**:
  - `neobrain.sandbox.roi_report(conn: sqlite3.Connection, *, days: int = 7, at_ms: Optional[int] = None) -> dict` computes the empirical ROI of the sandbox over the window `[at_ms - days*86400000, at_ms]` without ever disabling or removing anything:
    - `days`: `int` (clamped `>= 1`)
    - `runs`: `{"total": int, "ok": int, "skipped": int, "failed": int, "confirmed": int, "refuted": int, "inconclusive": int}` counted from `events` (`title="life: dream sandbox"`) in the window.
    - `lessons_created`: total count of `m_atoms` created in the window where `origin_session_id = 'sandbox'` or `source LIKE 'memory/dreaming/sandbox/%'`.
    - `served`: sum of `m_rank.served` across those sandbox atoms.
    - `used`, `useful`, `noise`, `unused`: total feedback signal counts from `m_feedback` across those sandbox atoms.
    - `lessons`: list of dicts for each sandbox atom (`id`, `label`, `text`, `hub`, `created`, `source`, `served`, `used`, `useful`, `noise`, `unused`).
  - `cli.py`: `neobrain sandbox [--replay YYYY-MM-DD] [--roi] [--days N]` — when `--roi` is passed, calls `sandbox.roi_report(conn, days=args.days)` and prints the JSON scorecard (exit 0).
  - `api.py`: `GET /api/mind/sandbox/roi?days=7` returns `sandbox.roi_report(conn, days=days)`.
- **Verification method**:
  - Automated tests in `tests/test_sandbox.py` seeding sandbox runs, sandbox lesson atoms, `rank.record_served` exposures, and `mind.record_feedback` (`used`, `useful`, `noise`) signals, then verifying `sandbox.roi_report`, `cli.main(["sandbox", "--roi", "--days", "7"])`, and `GET /api/mind/sandbox/roi?days=7`.

## File-Touch Manifest

### Production Paths (`neobrain`)
- `src/neobrain/sandbox.py` (new)
- `src/neobrain/config.py`
- `src/neobrain/settings_store.py`
- `src/neobrain/life.py`
- `src/neobrain/cli.py`
- `src/neobrain/api.py`

### Test Paths (`neobrain`)
- `tests/test_sandbox.py` (new)

## Exact Verify Command
```bash
~/neobrain/.venv/bin/python -m pytest -q --tb=short
```
