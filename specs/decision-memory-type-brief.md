# Slice 1 Brief — Decision-Model Memory Typing (`decision-memory-type`)

## Goal
Replace `neobrain/src/neobrain/mind.py`'s regex-based atom type guesser with `cloudflare/clef-flash` decision-model classification (`POST {llm_base_url without /v1}/systemone`) on `mind.remember()` and `mind._promote_daily_notes()`, with deterministic fail-open fallback to the existing regex rules when the gateway is unreachable, unconfigured, or errors.

## Acceptance Criteria

### AC-1: Decision Model Config & Runtime Settings (`config.py`, `settings_store.py`)
- **What it does**:
  1. `neobrain.config.Settings` defines `llm_model_decision: str = "cloudflare/clef-flash"` (env `NEOBRAIN_LLM_MODEL_DECISION`).
  2. `neobrain.settings_store.REGISTRY` includes a `Setting` entry with `key="llm_model_decision"`, `label="Decision model"`, `type="str"`, `section="cognition"`, exposing it in `settings_store.get_public(conn)` and allowing runtime updates via `PUT /api/settings`.
  3. `neobrain.runtime._resolve_model("decision")` resolves to `config.settings.llm_model_decision`.
- **Verification method**: Automated pytest test (`tests/test_decision_memory_type.py::test_ac1_decision_model_config_and_settings`) verifying `config.Settings().llm_model_decision == "cloudflare/clef-flash"`, `runtime._resolve_model("decision")`, and `PUT /api/settings` updating `llm_model_decision`.

### AC-2: Native `/systemone` Decision Client (`runtime.py`)
- **What it does**:
  1. `neobrain.runtime.decision_url() -> str` derives the System One endpoint from `runtime.base_url()` by stripping a trailing `/v1` (if present) and appending `/systemone` (e.g. `"http://127.0.0.1:4000/v1"` -> `"http://127.0.0.1:4000/systemone"`).
  2. `neobrain.runtime` defines Pydantic v2 request/response models at the wire boundary (`DecisionQuestion`, `DecisionRequest`, `DecisionAnswer`, `DecisionResponse`) per SPEC §10.
  3. `neobrain.runtime.classify_memory_type(text: str, *, model: str = "decision", timeout: float = 2.0) -> str | None`:
     - Makes a **single** HTTP `POST` attempt via `runtime._open(req, timeout)` (never sleeps or retries) to `decision_url()` with Bearer auth from `runtime._api_key()`.
     - Sends JSON body matching `DecisionRequest`: `model` resolved via `_resolve_model(model)`, `state` set to stripped `text`, and `questions["memory_type"]` with `type="choice"`, `instructions`, and `criteria` keys `("lesson", "decision", "preference", "observation")`.
     - Parses `DecisionResponse` and returns the selected category (`answers["memory_type"].choice` or `answers["memory_type"].value`, falling back to the highest-probability key in `answers["memory_type"].probabilities` if `choice`/`value` is absent) when it is one of `{"lesson", "decision", "preference", "observation"}`.
     - Fail-open: returns `None` (never raises) when `text` is blank, `_api_key()` is empty, `_resolve_model(model)` is empty, or `_open` raises `URLError`/`HTTPError`/`TimeoutError`/`OSError`, or the response JSON fails validation or lacks a valid category.
- **Verification method**: Automated pytest tests (`tests/test_decision_memory_type.py::test_ac2_*`) inspecting the captured `urllib.request.Request` URL, headers, and JSON body via monkeypatched `runtime._open`, testing valid responses (`choice`, `value`, `probabilities`) and verifying fail-open `None` return with zero `runtime._sleep` calls on HTTP 500, malformed JSON, `URLError`, and missing API key.

### AC-3: `mind.remember()` Classifies Default/Untyped Memories via `clef-flash` with Regex Fallback (`mind.py`)
- **What it does**:
  1. In `mind.remember(conn, text, *, atype="observation", ...)`, after `remember_rejection(text)`, `supersedes` validation, and `dedupe` early return:
     - When `atype in (None, "", "auto", "observation")`, `remember()` classifies the memory via `mind._classify_memory_type(cleaned, label=label)` (which formats `f"{label}: {cleaned}"` when `label` is provided and differs from `cleaned`, and calls `runtime.classify_memory_type`).
     - When `runtime.classify_memory_type` returns a valid category (`"lesson"`, `"decision"`, `"preference"`, or `"observation"`), `atype` is set to that category and `weight` (when not explicitly passed by caller) is computed via `_weight(atype)` (`0.85` for `"lesson"`, `0.88` for `"decision"`, `0.90` for `"preference"`, `0.62` for `"observation"`).
     - When `runtime.classify_memory_type` returns `None` (gateway offline/unconfigured/error), `remember()` falls back to `mind._regex_promotion_kind(label or "", cleaned) or "observation"`.
  2. When a caller explicitly passes a curated or special `atype` (`"lesson"`, `"decision"`, `"preference"`, `"open"`, `"dream"`, `"identity"`), `remember()` preserves `atype` as given and does **not** call `runtime.classify_memory_type`.
- **Verification method**: Automated pytest tests (`tests/test_decision_memory_type.py::test_ac3_*`) verifying:
  - A lesson/decision/preference passed to `mind.remember(conn, text)` (and via `POST /api/mind/remember` with omitted `type` or `type="observation"`) is stored with `type` = `"lesson"` / `"decision"` / `"preference"` and matching `_weight` when `clef-flash` responds.
  - True observations stay `type="observation"` when `clef-flash` returns `"observation"`.
  - Explicit non-observation `atype` (`atype="preference"`, `atype="lesson"`, `atype="open"`, etc.) does not invoke `runtime.classify_memory_type`.
  - When `runtime.classify_memory_type` returns `None`, `remember()` falls back to `_regex_promotion_kind` and defaults to `"observation"`.

### AC-4: `mind._promote_daily_notes()` & `mind._promotion_kind()` Use `clef-flash` with Caching and Regex Fallback (`mind.py`)
- **What it does**:
  1. `mind._regex_promotion_kind(heading: str, body: str) -> str | None` preserves the exact pre-existing regex heuristic (`_PROMOTE_DENY`, `_PROMOTE_LESSON`, `_PROMOTE_LESSON_BODY`, `_PROMOTE_HEADING`, `_PROMOTE_BODY`).
  2. `mind._promotion_kind(atype: str, heading: str, body: str) -> str | None`:
     - Returns `atype` immediately if `atype in ("decision", "lesson")`.
     - Returns `None` immediately if `atype != "observation"`, or if `heading` or `body` is empty after `_clean`, or if `_PROMOTE_DENY.match(h)` matches.
     - Calls `_classify_memory_type(b, label=h)`:
       - Returns `"lesson"` when the classifier returns `"lesson"`.
       - Returns `"decision"` when the classifier returns `"decision"` or `"preference"`.
       - Returns `None` when the classifier returns `"observation"` (does **not** fall back to regex when the decision model explicitly judged the section to be an observation, preventing keyword false positives).
       - Falls back to `_regex_promotion_kind(h, b)` **only** when `_classify_memory_type` returns `None` (gateway unreachable/unconfigured/error).
  3. In `_promote_daily_notes(conn, cap=PROMOTE_CAP, *, embed=True)`:
     - Checks `if r["type"] in ("decision", "lesson"): continue` and `if len(body) < 40: continue` **before** calling `_promotion_kind` so already-curated or short sections never trigger a classifier call.
  4. `mind._CLASSIFY_CACHE` caches non-`None` classifier verdicts by `(model, content_hash(state))` (bounded to 2048 entries, cleared via `mind.clear_classify_cache()`) so repeated `consolidate()` runs do not re-query `/systemone` for unchanged day-note sections, while `None` (error/offline) is never cached.
- **Verification method**: Automated pytest tests (`tests/test_decision_memory_type.py::test_ac4_*`) and E2E live gateway check verifying:
  - A daily note (`a_*`, `source="memory/2026-10-29.md"`, `type="observation"`) containing a real root-cause lesson or architectural choice without regex keywords is promoted to `m_pd_*` with `type="lesson"` or `"decision"` and a `derived-from` edge when `clef-flash` responds.
  - A status note containing incidental keywords (e.g. `"built"` or `"switched"`) is **not** promoted when `clef-flash` classifies it as `"observation"`.
  - When the gateway is unreachable (`classify_memory_type` returns `None`), `_promote_daily_notes` falls back to `_regex_promotion_kind` and promotes regex-matching notes deterministically.
  - Calling `_promote_daily_notes` twice on the same unchanged daily note invokes `runtime._open` only once (cache hit on second pass), whereas a failed (`None`) call is not cached and retries on the next pass.
  - E2E verification against live `http://127.0.0.1:4000/systemone` with `cloudflare/clef-flash` classifying both `mind.remember()` and `mind._promote_daily_notes()` in a temporary SQLite DB.

## File-Touch Manifest

### Production Paths
- `~/neobrain/src/neobrain/config.py`
- `~/neobrain/src/neobrain/settings_store.py`
- `~/neobrain/src/neobrain/runtime.py`
- `~/neobrain/src/neobrain/mind.py`

### Test Paths
- `~/neobrain/tests/test_decision_memory_type.py`

## Verification Commands
- Target slice tests: `~/neobrain/.venv/bin/python -m pytest tests/test_decision_memory_type.py -q --tb=short` (run in `~/neobrain`)
- Full test suite: `~/neobrain/.venv/bin/python -m pytest -q --tb=short` (run in `~/neobrain`)
