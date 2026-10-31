# Run Log — Decision-Model Memory Typing (`decision-memory-type`)

## 2026-10-29 — RECON & Slice 1 Decision

### Empirical Findings from Pre-Build Experiments (Session `ses_e7906c982ffej3BSSjGEMvqlHh`)
1. **Endpoint & Latency**: `POST http://127.0.0.1:4000/systemone` with `model: "cloudflare/clef-flash"` and `questions.memory_type` (`type: "choice"`, criteria: `lesson`, `decision`, `preference`, `observation`) answered 24/24 real `neobrain.db` atoms with `200 OK`, `output_tokens: 0`, median latency **346 ms** (min 250 ms, max 921 ms), ~230 input tokens/call (~$0.000009/atom).
2. **Accuracy vs Regex (`mind._promotion_kind`)**:
   - On 8 real daily-note (`a_*`) atoms from `memory/2026-*.md` that `mind.py` regexes left stranded as `observation`: `cloudflare/clef-flash` accurately recovered all 6 missed lessons/decisions (`a_56db4dcdabc0` `lesson` 0.85, `a_5c17b5868286` `lesson` 0.94, `a_bcfb1e2ca401` `lesson` 0.54, `a_b021aa160c66` `lesson` 0.83, `a_f4507a13eff8` `decision` 0.97, `a_71d0bc49bd3e` `decision` 0.99) and kept the 2 true status logs (`a_477bdf1af1ea` 0.89, `a_9bc9aa2598e6` 0.87) as `observation`.
   - Across all 24 sampled atoms (8 stranded `a_*` notes, 8 stored `lesson` atoms, 8 stored `decision`/`preference` atoms): 0 false negatives and 0 false positives on curated vs non-curated (`observation`) classification.

### Local Repository Recon (the repo root)
- `src/neobrain/config.py`: `Settings` holds `llm_base_url` (default `"http://127.0.0.1:4000/v1"`), `llm_api_key`, `llm_model_cheap`, `llm_model_strong`.
- `src/neobrain/settings_store.py`: `REGISTRY` exposes editable `cognition` settings in the Observatory UI (`llm_base_url`, `llm_api_key`, `llm_model_cheap`, `llm_model_strong`).
- `src/neobrain/runtime.py`: Native stdlib (`urllib.request`) + Pydantic client for the LiteLLM gateway; uses `_open(req, timeout)` so tests can inject fake gateway responses hermetically without network calls.
- `src/neobrain/mind.py`:
  - `remember(conn, text, *, atype="observation", ...)` (lines 849–921) defaults `atype` to `"observation"` without classifying the text, so any caller (`mind.remember`, `POST /api/mind/remember`, `neobrain remember`, or `memory_store` tool) that omits `type` or passes default `"observation"` stores the memory as `"observation"` (which `neoBrain-memory` excludes from ordinary per-turn `CURATED` recall).
  - `_promotion_kind(atype, heading, body)` (lines 1598–1617) and `_promote_daily_notes(conn, cap=PROMOTE_CAP, *, embed=True)` (lines 1634–1741) rely solely on regex patterns (`_PROMOTE_LESSON`, `_PROMOTE_LESSON_BODY`, `_PROMOTE_HEADING`, `_PROMOTE_BODY`) to classify `observation`-typed day-note sections into `"lesson"` or `"decision"`.

### Slice 1 Decision
Replace `mind.py`'s regex type-guesser on `remember()` and `_promote_daily_notes()` with `cloudflare/clef-flash` via `/systemone`, with fail-open fallback to the regex heuristic when the gateway is unreachable or unconfigured:
1. **Config & Settings (`config.py`, `settings_store.py`)**:
   - Add `llm_model_decision: str = "cloudflare/clef-flash"` to `config.Settings` (`NEOBRAIN_LLM_MODEL_DECISION`).
   - Add `Setting("llm_model_decision", "Decision model", "str", "cognition", ...)` to `settings_store.REGISTRY`.
2. **Native System One Decision Client (`runtime.py`)**:
   - Add Pydantic request/response models (`DecisionQuestion`, `DecisionRequest`, `DecisionAnswer`, `DecisionResponse`) for `POST /systemone`.
   - Add `decision_url() -> str` (strips trailing `/v1` from `base_url()` and appends `/systemone`).
   - Resolve tier `"decision"` to `config.settings.llm_model_decision` in `_resolve_model`.
   - Add `classify_memory_type(text: str, *, model: str = "decision", timeout: float = 2.0) -> str | None`: single-attempt (no backoff sleep so offline/test calls fail in <1ms), sends `state=text` and the `memory_type` choice question (`lesson`, `decision`, `preference`, `observation`), returns the chosen category if valid or `None` on any config/network/HTTP/validation error (never raises).
3. **Memory Type Classification in `remember()` and `_promote_daily_notes()` (`mind.py`)**:
   - Add bounded in-memory cache `_CLASSIFY_CACHE` (keyed by `(model, content_hash(state))`, caching only non-`None` verdicts, with `clear_classify_cache()` helper) so repeated `consolidate()` passes never re-query unchanged day-note sections.
   - Extract `_regex_promotion_kind(heading: str, body: str) -> str | None` preserving the exact pre-existing regex rules as the offline fallback.
   - Update `_promotion_kind(atype: str, heading: str, body: str) -> str | None`:
     - Keep fast exits: `if atype in ("decision", "lesson"): return atype`; `if atype != "observation": return None`; `if _PROMOTE_DENY.match(h): return None`.
     - Query `_classify_memory_type(b, label=h)` via `runtime.classify_memory_type`:
       - When `clef-flash` returns `"lesson"`, return `"lesson"`.
       - When `clef-flash` returns `"decision"` or `"preference"`, return `"decision"`.
       - When `clef-flash` returns `"observation"`, return `None` (do NOT fall back to regex when the decision model explicitly judged it an observation).
       - Only when `clef-flash` returns `None` (gateway unreachable / unconfigured / error) fall back to `_regex_promotion_kind(h, b)`.
   - In `_promote_daily_notes`: check `r["type"] in ("decision", "lesson")` and `len(_clean(r["text"] or "")) < 40` before calling `_promotion_kind` so short or already-curated sections never trigger a classification call.
   - Update `remember(conn, text, *, atype="observation", ...)`:
     - After `remember_rejection(text)`, `supersedes` validation, and `dedupe` early-return, when `atype in (None, "", "auto", "observation")`, call `_classify_memory_type(text, label=label)`:
       - If `clef-flash` returns `"lesson"`, `"decision"`, `"preference"`, or `"observation"`, use that as `atype` (and compute `weight` via `_weight(atype)` if `weight is None`).
       - If `clef-flash` returns `None` (offline fallback), set `atype = _regex_promotion_kind(label or "", text) or "observation"`.
     - When a caller explicitly passes a non-observation type (`"lesson"`, `"decision"`, `"preference"`, `"open"`, `"dream"`, `"identity"`), preserve it unchanged without calling `classify_memory_type`.

### Rejected Alternatives
- **Calling chat completions (`/v1/chat/completions`) for classification**: Rejected because `cloudflare/clef-flash` on `/systemone` is purpose-built for zero-output-token typed classification (`output_tokens: 0`, 346 ms median, $0.000009/call) and does not suffer from reasoning-token exhaustion.
- **Multi-attempt backoff sleeps inside `classify_memory_type`**: Rejected because `remember()` is on the synchronous write path and hermetic tests run with `llm_base_url = "http://127.0.0.1:9/v1"`; a single attempt with fail-open fallback keeps offline unit tests sub-millisecond and guarantees memory writes never fail when the gateway is down.
- **Caching `None` on failure**: Rejected because a transient gateway outage or an unmocked test call would poison the cache for subsequent calls after recovery.

## 2026-10-29 — Slice 1 Gates, QA Validation & Task Review

### RED Gate
- Spawned `qa` in AUTHOR mode (`ses_e78cac5bcffeywqRhcaZs7nlVz`), creating `tests/test_decision_memory_type.py` (20 test cases across AC-1..AC-4).
- Orchestrator RED gate command: `.venv/bin/python -m pytest tests/test_decision_memory_type.py -q --tb=short`
- Result: exit code `1` (`20 failed, 1 warning in 2.76s`), all `MISSING_BEHAVIOR` / `ASSERTION_MISMATCH`.

### BUILD & GREEN Gate
- Spawned `builder` (`ses_e78c1acabffed7tAM27tDOKPYp`), modifying `src/neobrain/config.py`, `src/neobrain/settings_store.py`, `src/neobrain/runtime.py`, and `src/neobrain/mind.py`.
- Test integrity check: `tests/test_decision_memory_type.py` untouched by builder.
- Syntax/compile check (`python -m py_compile`): exit code `0`.
- Full test suite (`.venv/bin/python -m pytest -q --tb=short`): exit code `0` (`424 passed, 1 warning in 54.71s`).

### QA VALIDATE & E2E Evidence
- Spawned `qa` in VALIDATE mode (`ses_e78b66237ffelMrrhT80nhz0wo`).
- Target test suite (`pytest tests/test_decision_memory_type.py -v --tb=short`): exit code `0` (`20 passed in 2.91s`).
- Live E2E check against `http://127.0.0.1:4000/systemone` (`cloudflare/clef-flash`) in a temp SQLite store: exit code `0` — verified `mind.remember()` classified untyped lesson/decision/preference/observation inputs with weights `0.85`, `0.88`, `0.90`, `0.62`, and `mind._promote_daily_notes()` promoted a semantic lesson day-note without regex keywords (`m_pd_ef362ea7a07f` -> `derived-from` `a_e2e_note_1`) while suppressing a status observation containing incidental `"built"`/`"switched"` keywords.

### Reviewer TASK-REVIEW
- Spawned `reviewer` in TASK-REVIEW mode (`ses_e78940ddcffeiTsLsoKAzSuP1v`).
- Verdict: `PASS` (0 blockers, 20/20 slice tests and 424/424 full suite verified independently).

### Reviewer FINAL-RECONCILIATION
- Spawned `reviewer` in FINAL-RECONCILIATION mode (`ses_e788dc8bcffez5UlQVayWUav8c`).
- Independently executed AC-1..AC-4 verification suites (`20 passed in 4.23s`, exit code `0`), live `/systemone` E2E check (`1 passed in 2.93s`, exit code `0`), and full regression suite (`424 passed in 51.94s`, exit code `0`).
- Verdict: `PASS`.


