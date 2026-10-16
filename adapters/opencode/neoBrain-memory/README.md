# neoBrain-memory (OpenCode adapter)

Thin OpenCode plugin: session wakeup injection, per-turn recall lane, the
`memory_store` / `memory_open` / `memory_search` / `memory_rate` tools, and the persona lane —
injects `SOUL.md` / `IDENTITY.md` / `USER.md` into the system prompt once per
session (caps 20k chars/file, 60k total, USER.md 4k).
`USER.md` is compacted before the cap applies — the header/format explainer,
fenced examples, dated metadata comments, `superseded` entries, inline `[pin]`
markers and the `Related` footer are stripped, leaving only the active directive
bullets — so the ledger can grow without its tail being silently truncated.
The persona lane is local file I/O and independent of the daemon. Port of
`~/.opencode/plugins/timeline-memory/index.ts` (SPEC §8) plus the former
standalone `persona-bootstrap` plugin (merged 2026-10-01). No logic beyond protocol.

Single file (`index.ts`), loaded automatically from the opencode plugins
directory — same pattern as the old timeline plugin. No-op when the daemon is down.

## Install

Installed and live since 2026-09-30: `~/.opencode/plugins/neobrain-memory`
symlinks to this directory. Caution: never run two memory plugins at once —
memory would be injected twice.

```bash
# requires the neoBrain daemon running
ln -s /path/to/neobrain/adapters/opencode/neoBrain-memory ~/.opencode/plugins/neobrain-memory
```

Typecheck: `cd adapters/opencode/neoBrain-memory && npx tsc --noEmit` (config in `tsconfig.json`).
Tests: `node --test index.test.ts` (Node 22.6+ with type stripping; no dependencies, no running daemon).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `NEOBRAIN_API` | `http://127.0.0.1:9192` | Base URL of the neoBrain daemon (mind API) |

## In-turn rating (SPEC §5)

- The recall block carries one advisory line: call `memory_rate(id, "useful" \| "noise")` when a recalled memory helped or was wrong. It is a hint, never a gate, and useful/noise verdicts are never inferred — they stay the model's.
- The recall block inlines the text of the top hit. That atom (and only that one — atoms merely listed by id do not count) is posted as `used` automatically, best effort and once per turn, and is excluded from the `unused` inference. A failed post never affects injection.
- Rating is in-session and by the agent that used the memory: there is no cross-session state and no blocking gate. The former "unrated protocol" (a global pending ledger that blocked mind calls until rated) was removed — it forced a session to rate atoms another session had surfaced, and drove ratings to a near-constant `useful`.
- Objective usage is still automatic: `memory_open(id)` posts a `used` signal on success.
- Ratings are recorded against the rater's session *and* the memory's origin session (`m_atoms.session_id`, `m_feedback.origin_session_id`), so provenance is preserved on both ends.

## Store with origin session

`memory_store` is for **one durable fact** — a decision, preference, correction,
or lesson worth recalling later. The tool description tells the agent not to
call it for chatter or status ("ok", "thanks", "got it", "done", "continuing")
and not to retry a text the server refused.

The server enforces that rule (`POST /api/mind/remember` → 422, see the repo
README's "Memory writes"). The tool surfaces it as
`memory_store: not stored (filler) — <reason>` rather than a bare HTTP status,
so the agent can tell a rejection from a daemon failure. A down daemon is still
reported as an error and never crashes the session.



- `memory_store(text, type?, hubs?, source?, label?, dedupe?)` POSTs `/api/mind/remember` with `session` = the live OpenCode sessionID, so the atom's `m_atoms.session_id` records **which session it came from**.
- Prefer this over the CLI for agent saves: a `neobrain remember` from the shell lands as `session_id="cli"` unless `--session <ses_id>` is passed (the origin link was previously near-empty for exactly this reason).
