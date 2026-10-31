# One-Line Brief — Cloudflare preview pipeline (`cf-preview-pipeline`)

**Fix:** Add an empty `"previews": {}` block to `wrangler.jsonc` (top level, alongside `assets`) so Cloudflare Workers Builds' per-branch preview builds (`npx wrangler preview`, auto-triggered per PR) stop failing with "Your Wrangler configuration is missing a `previews` block" (root cause verified live from build `50ea96f3-0af4-445d-b7a6-50a9c403f093` logs; per PR #17 lesson m_d5277138a343).

**Verify:** `.venv/bin/python -m pytest -q --tb=short` still exits 0 (config is not Python-tested) + the "Workers Builds: neobrain" check on PR #30 turns success after push (observable check).
