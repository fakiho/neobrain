# Run Log — Dedicated LiteLLM key for neobrain (`neobrain-dedicated-key`)

## 2026-10-29 — Decision

- User request: neobrain must stop using a shared key (user called it "the master key"; masked read shows `sk-E****`, a virtual key rather than the gateway master key — builder confirms the live alias via `/key/info`) and get a dedicated key for both decision (`/systemone`) and LLM calls.
- Design: `POST /key/generate {"key_alias": "neobrain"}` — **alias only, no `models` scope**: INFRA_MAP §liteLLM (2026-10-25) proved a scoped key 403s `key_model_access_denied` on any router hop missing from its list, and the complexity router hops across `go/*`/`deepseek/*`/`deepinfra/*`/gemini tiers plus the new haiku fallback — enumerating hops would be brittle.
- Single key covers both paths: `runtime._api_key()` feeds `runtime.chat` and `classify_memory_type` alike (`NEOBRAIN_LLM_API_KEY`).
- `/api/settings` DB override check: `settings_store` layers `app_settings` on top of env — if `llm_api_key` is set in the live store's `app_settings`, it must be updated there too or the `.env` swap is a no-op.
  Empirical: `app_settings` was empty, so `.env` alone governs (later re-confirmed live: `/api/settings` → `llm_api_key: {set: true, overridden: false, from_env: true}`).
- `.env` chmod 644 → 600 while touching it (secrets red line). Old key stays (used by other callers; nothing revoked).
- Daemon `neobrain.service` restart required for the env change; verify `/api/health` after.

## 2026-10-29 — Empirical Correction & Build Result (`ses_e77d80fd4ffeBk0g5kMkiZMOmJ`)

- `/key/info` on the old `.env` key (`sk-E…z2dg`) returned `key_alias: "NeoBrain"`, created `2026-09-20`, spend `7.459` — the same key that was `OpenClaw2` on 2026-10-13 ($7.24 then), renamed in the UI since. Not the master key. What used the master key were this session's ad-hoc E2E probes, which passed the master bearer inline instead of calling `runtime._api_key()`.
- `app_settings` in `neobrain.db` had no `llm_api_key` row, so `.env` alone governs.
- Minted dedicated `key_alias: "neobrain"` (`sk-Q…rNCQ`, unscoped `models: []`), wrote to `neobrain/.env`, `chmod 600`, restarted `neobrain.service` (`/api/health` → 200).
- Verified in fresh process: `classify_memory_type` → `lesson` (exit 0), `runtime.chat(model="cheap")` → `ok` (exit 0), `/key/info` on new key → `spend: 3.094e-05` (> 0). Old `NeoBrain` key untouched in `/key/list`.

## 2026-10-29 — Reviewer Remedy Round 1 (blockers closed by orchestrator)

- Reviewer (`ses_e77cb3fbbffecAX5rS5frqRCgw`) verdict `BLOCKED` on two items its sandbox could not execute (its shell was restricted to git + HTTP; core swap was proven live by the reviewer itself: daemon probe Δspend +2.7e-05 billed to `neobrain`).
- Blocker 1 (`.env` mode): orchestrator ran `stat -c '%a %U' /home/sparo/neobrain/.env` → `600 sparo` (exit 0). CLOSED.
- Blocker 2 (literal fresh-process dual call, clef-flash was transiently 400 upstream `CloudflareException 5007` — affected every key, not a key-scope issue): orchestrator re-ran after recovery, `/home/sparo/neobrain/.venv/bin/python -c` with both calls → `classify: lesson | chat: ok`, exit 0. CLOSED.
- Reviewer [WARN]s recorded, not acted on: (a) both working trees carry unrelated uncommitted slices — split hunks at commit time; (b) ~~third similarly-named key `neobrain mac` (`sk-…ycwA`, $0.748) exists — rename/delete if stale~~ **CORRECTED by user 2026-10-29: `neobrain mac` is a live per-device key for the Mac, not residue.** One alias per app×machine is the intended pattern — never consolidate or revoke a second neobrain-named key without checking which machine it serves.


