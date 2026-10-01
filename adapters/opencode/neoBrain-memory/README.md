# neoBrain-memory (OpenCode adapter)

Thin OpenCode plugin: session wakeup injection, per-turn recall lane, the
`memory_open` / `memory_search` / `memory_rate` tools, and the persona lane —
injects `SOUL.md` / `IDENTITY.md` / `USER.md` into the system prompt once per
session (OpenClaw bootstrap parity; caps 20k chars/file, 60k total, USER.md 4k).
The persona lane is local file I/O and independent of the daemon. Port of
`~/.opencode/plugins/timeline-memory/index.ts` (SPEC §8) plus the former
standalone `persona-bootstrap` plugin (merged 2026-10-01). No logic beyond protocol.

Single file (`index.ts`), loaded automatically from the opencode plugins
directory — same pattern as the old timeline plugin. No-op when the daemon is down.

## Install

Not installed yet — cutover is a later sprint. The live timeline plugin stays in
place until then; never run both at once (memory would be injected twice).

```bash
# requires the neoBrain daemon running (S6; `neobrain init` will automate this)
ln -s /home/sparo/neobrain/adapters/opencode/neoBrain-memory ~/.opencode/plugins/neobrain-memory
```

Typecheck: `cd adapters/opencode/neoBrain-memory && npx tsc --noEmit` (config in `tsconfig.json`).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NEOBRAIN_API` | `http://127.0.0.1:9192` | Base URL of the neoBrain daemon (mind API) |
| `NEOBRAIN_RATING_GRACE_SECONDS` | `120` | How long a served memory may stay unrated before mind calls get blocked |
| `NEOBRAIN_RATING_TIMEOUT_SECONDS` | `600` | After this, an unrated memory auto-clears (exposure with no verdict) |

## Unrated-feedback protocol (SPEC §5)

- Every recall/search result surfaced to the model registers its atom ids as `pending` in an in-process map (id → first-served timestamp); `memory_rate` clears an entry immediately, valid verdict first, POST outcome irrelevant.
- While a pending atom is older than the grace window, the next mind call (`memory_open`, `memory_search`, or the per-turn recall injection) returns an explicit BLOCKING notice listing the pending ids instead of results — nothing is silently dropped; the model rates with `memory_rate(id, "useful" \| "noise")` and retries. `memory_rate` itself is never gated.
- Pending atoms older than the timeout are swept automatically (lazily, on each mind call — no background timer) and count as exposure with no verdict: no POST, since the mind API has no exposure endpoint yet (S4/S6 may add one).
