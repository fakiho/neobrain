# Brief — Dedicated LiteLLM key for neobrain (`neobrain-dedicated-key`)

**Goal:** replace neobrain's shared LiteLLM credential (`.env NEOBRAIN_LLM_API_KEY`, currently alias `OpenClaw2` per INFRA_MAP §liteLLM) with a dedicated virtual key (alias `neobrain`, no model scope — per the `node-red-notify-gate` 403 lesson), used by both the chat path (`runtime.chat`, models `DeepseekClaudeComplexity` / `deepseek/deepseek-v4-pro`) and the decision path (`/systemone`, `cloudflare/clef-flash`); restart the daemon; verify spend attributes to the new key; tighten `.env` to 600; update INFRA_MAP.

**Verify:** live — `runtime.classify_memory_type` (decision) + `runtime.chat` (cheap) both 200 under the new key; `GET /key/info` shows alias `neobrain` with spend > 0; `/api/health` 200 after daemon restart; old key still exists (nothing revoked).

**Manifest:** `/home/sparo/neobrain/.env` (one line + chmod 600), `/home/sparo/INFRA_MAP.md` (line-109 attribution note). No tests, no code.
