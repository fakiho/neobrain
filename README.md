# neoBrain

A simulated brain for AI agents: durable memory, rank-based forgetting,
nightly dreams and a self-authored soul — one local daemon, one dashboard.
The user watches; the agent becomes.

Spec: [`SPEC.md`](./SPEC.md) — the frozen contract.
Family: [alionix](https://alionix.com) — neohive (agents collaborate) + neoBrain (agents remember).

## Screenshots

**Brain** — the mind graph: atoms, hubs and typed edges.
![Brain graph view](docs/screenshots/brain.png)

**Debug** — live request trace and every plugin lane injection, for answering
"is the memory actually firing right now".
![Debug page](docs/screenshots/debug.png)

**Dreams** — what the nightly consolidation pass wrote into the diary.
![Dreams view](docs/screenshots/dreams.png)

## What it is

Durable memory for AI agents, built as a mind model rather than a log:

- **Memory** — atoms + hubs + typed edges in SQLite. Hybrid recall (lexical +
  cosine vectors, local Ollama embeddings by default); top-K **by rank within
  a token budget**, never the whole store.
- **Rank** — deterministic, no model discretion:
  `quality = clamp01(feedback · decay · conn)`. Feedback (`used | useful |
  noise`) is the only factor that can reach zero; knobs live in `config.py`.
- **Forgetting** — rank-based only. Low rank + low exposure → *ignored*
  (never surfaced, never deleted); low rank + high exposure → *archived*
  (unretrievable but recoverable). Nothing is deleted by the economy;
  `neobrain forget` is the manual override.
- **Dreams** — nightly light/rem/deep replay consolidates top-ranked atoms
  into summaries, associations and the `DREAMS.md` diary. Dreams never decide
  retention.
- **Soul** — `IDENTITY.md` / `SOUL.md` are authored exclusively by the
  agent's own reflect/dream phases from its own experience; safety and spend
  red lines are immutable.
- **Life loop** — an in-process state machine in the daemon:
  perceive (ingest + health probes) → reflect (consolidate) → act (one
  internal thought, stored) → rest (dreams at night). Cadence lives in code
  and the DB, not in OS timers.

## Architecture

```
 OpenCode ── neobrain-memory plugin ──┐        Observatory SPA (web/dist)
 (the agent's hands)   lanes + tools  │              ▲
                                      ▼              │
                        ┌────────────────────────────┴───┐
                        │ neoBrain daemon  :9192 (FastAPI)│
                        │  life loop (in-process thread): │
                        │  perceive → reflect → act → rest│
                        │  mind: recall · rank · forget   │
                        │  runtime: native LLM calls      │
                        │  ingest: opencode · git · docs  │
                        └───────────────┬────────────────┘
                                        ▼
                        SQLite store  (NEOBRAIN_DATA_DIR/neobrain.db, WAL)
```

- **Daemon** — FastAPI + uvicorn, default bind `0.0.0.0:9192`
  (`NEOBRAIN_BIND`). Owns the life loop; serves both the JSON API and the
  built dashboard from `web/dist`.
- **Ingest adapters** — `src/neobrain/ingest/`: the OpenCode session DB, git
  repos, and workspace docs; the life loop's perceive phase runs them, or
  `neobrain ingest` one-shot.
- **Rank layer** — `rank.py`: the bounded feedback·decay·conn formula above,
  plus exposure counters (`served + 3·interacted`).
- **Observatory** — React/Vite SPA (`web/src/brain/`): Brain graph, Memory
  feed, Dreams, Reader, Debug. Views deep-link by hash — `/#debug`,
  `/#dreams` — which is handy for jumping straight at the live trace.

## The OpenCode integration

Thin plugin: [`adapters/opencode/neoBrain-memory`](./adapters/opencode/neoBrain-memory/README.md)
(single `index.ts`, no logic beyond protocol; no-op when the daemon is down).
The platform **pushes** memory into context deterministically and exposes the
same mind for **pull**:

- **Wake-up pack** — once per session: identity/rules, open loops, most
  active hubs (`/api/mind/wakeup`).
- **Persona lane** — once per session, injects the workspace `SOUL.md` /
  `IDENTITY.md` / `USER.md` (caps 20k chars/file, 60k total, USER.md 4k).
  Local file I/O, deliberately independent of the daemon.
- **Standing directives** — pinned `preference` atoms rendered as a compact
  imperative block and **re-injected on every model call**, so time, context
  drift and compaction cannot drop the must-follow rules (weak models keep
  obeying). Kept tiny on purpose (8 lines / 1000 chars, tunable); sits in the
  cacheable system prefix; push-only, so it never trips the rating gate.
- **Per-turn recall** — the user's message is recalled against the mind and a
  compact rank-ordered index is injected (top 10 one-liners + the #1 hit's
  text). Curated types (preference/lesson/decision) auto-inject on ordinary
  turns; memory-intent phrasing escalates to deep recall.
- **Unrated-feedback protocol** — every surfaced atom registers as pending;
  past a grace window (120s) the next mind call gets an explicit blocking
  notice instead of results; past the timeout (600s) it auto-clears as
  exposure with no verdict. Deterministic — the critical path never depends
  on model choice.
- **Compaction hook** — the directives block is also injected into the
  summary request, so rules survive a compact; once-per-session gates reset
  so persona + wake-up re-inject next turn.
- **Pull tools** — `memory_open(id)`, `memory_search(query)`,
  `memory_rate(id, useful | noise)`.
- **Injection logging** — every lane POSTs what it pushed to
  `/api/debug/injections`; the Debug page renders the daemon's one blind spot.

## Quickstart

Python 3.12. Nothing leaves the host by default (local Ollama embeddings;
BYOK OpenAI-compatible endpoint for the brain's own cognition).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -e .          # add ,[dev] for pytest
cp .env.example .env                # then fill NEOBRAIN_LLM_* / embeddings

neobrain init                       # create the store + print next steps
neobrain init --install-plugin      # also symlink the opencode plugin (below)
neobrain start                      # daemon: API + dashboard + life loop
neobrain ui                         # print and open the Observatory URL
```

Dashboard defaults to `http://127.0.0.1:9192` (`NEOBRAIN_API`); the OpenAPI
schema is at `/api/schema`.

**OpenCode plugin** — symlink the adapter into the plugins directory (or let
`neobrain init --install-plugin` do it):

```bash
ln -s /path/to/neobrain/adapters/opencode/neoBrain-memory \
      ~/.opencode/plugins/neobrain-memory
```

Never run it alongside the old `timeline-memory` plugin — two memory plugins
would both inject and double-rate memories.

## CLI

`neobrain --help` lists everything; the main subcommands:

| Command | Purpose |
|---|---|
| `init` | create the store; `--install-plugin` symlinks the OpenCode plugin |
| `start` | run the daemon (API + dashboard + life loop) |
| `ui` | print and open the dashboard URL |
| `ingest` | run ingest adapters one-shot (`--source`, default all) |
| `remember` | store a memory (`--type --hubs --link kind:target --label --dedupe`) |
| `recall` | recall a relevant subgraph (`--limit --json --lexical`) |
| `wakeup` | compact wake-up pack (identity + open loops + recent) |
| `pin` | pin/unpin an atom as a standing directive (`--off`) |
| `directives` | show the standing directives re-injected every turn |
| `embed` | backfill local embeddings (idempotent; `--force --limit`) |
| `dreams` | run a dream pass (`--phase light\|rem\|deep\|all`, `--replay YYYY-MM-DD`) |
| `reflect` | run the weekly reflection pass (re-authors the persona docs) |
| `forget` | delete a memory by id |
| `link` | wire two memories (`kind`: supersedes, derived-from, caused-by, consolidates, about) |
| `consolidate` | dream pass on demand (merge hubs, promote patterns) |
| `feedback` | record a quality signal (`used\|useful\|noise`) |

## API

Read-mostly JSON; groups worth knowing (full schema at `/api/schema`):

- `/api/mind/*` — `stats`, `graph`, `atom/{id}`, `activity`, `memories`,
  `recall`, `wakeup`, `directives`, `pin`, `remember`, `feedback`,
  `consolidate`, `dreams`, `reader`, `import`.
- `/api/debug/*` — `requests` (rolling trace of every `/api` call),
  `injections` (plugin lane pushes, GET/POST), `overview` (rank states,
  mind ops, feedback, ingest runs, life phases, self/soul trace, config).
- Plus `/api/health`, `/api/stats`, `/api/events`, `/api/sessions`,
  `/api/search`, `/api/docs`, `/api/ingest/run`.

## Testing

```bash
.venv/bin/python -m pytest -q
```

117 tests passing (verified 2026-10-02), including rank calibration and
parity tests against the old timeline oracle (`tests/parity/`).

## Status

Active development. Sprints S0–S7 are done (core mind, Observatory, plugin,
rank, life loop, API/CLI wiring, cutover — see `SPEC.md` §14 for the board
and [`docs/CUTOVER.md`](./docs/CUTOVER.md) for the migration). S8 (npm
launcher + PyInstaller packaging) is pending, so there is no published
package yet — install from this repo.

## License

MIT
