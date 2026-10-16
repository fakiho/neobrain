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
  cosine vectors, local Ollama embeddings by default); top-K by relevance
  within a token budget — never the whole store, and forgotten atoms are
  never candidates. See [Hybrid recall](#hybrid-recall).
- **Rank** — deterministic, no model discretion:
  `quality = clamp01(feedback · decay · conn)`. Feedback (`used | useful |
  noise`) is the only factor that can reach zero; the inferred `unused`
  hint is a recall drag, not a verdict, so it does not move quality. Knobs
  live in `config.py`.
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
  internal thought, stored) → rest (dreams at night). The same loop
  recomputes rank and demotes forgotten atoms once a day
  (`NEOBRAIN_RANK_INTERVAL_MINUTES`, default 1440). Cadence lives in code
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
  feed, Dreams, Reader, Debug, Settings. Views deep-link by hash — `/#debug`,
  `/#settings`, `/#dreams` — which is handy for jumping straight at the live
  trace or the controls.
- **Settings tab** — enable/disable the rituals (dreams, identity/soul-reflect,
  consolidation, internal thoughts, rating watch), tune the schedule, and point
  cognition at a provider (in-house LiteLLM, LLM Gateway, OpenAI, Ollama, or
  custom) with a key + model picked from the provider's own `/models`. Writes
  are stored as overrides over `.env` (`app_settings`, schema v5) and applied
  live — only the daemon's address fields (Bind, API URL) wait for a restart,
  and saving one says so.

## Hybrid recall

Search is two scorers blended, not one. Every active atom is ranked by:

```
score = lexical_norm + α · cosine_norm          (α = 2.0, `recall_alpha`)
```

- **Lexical** — a small BM25-flavoured model: IDF weighting, term-frequency
  saturation, field weights (label 3.0 · tags/hub 2.5 · text 1.0), a hub-anchor
  bonus (query hubs count, synonyms included) and length normalisation. This
  half anchors exact ids, paths and rare tokens.
- **Semantic** — cosine similarity between the query vector and each atom's
  embedding (label + body), min-max stretched over the candidate set so both
  terms share a 0..1 scale for *any* embedding provider. That normalisation is
  what makes α portable across models.

Vectors earn their keep on paraphrases — "the thing that watches the front
door" surfaces the doorbell atom even with no word overlap — while lexical
still wins on identifiers. Relevance dominates: feedback can only nudge a
score by ±10%.

**Degradation is graceful.** Embeddings are best-effort everywhere: vectors
live in an `m_embeddings` side table (L2-normalised float32, so cosine is a
plain dot product — no numpy dependency), atoms embed at `remember`/ingest
time, and if the provider is down or unconfigured recall silently falls back
to lexical-only. A memory write can never fail because of embeddings.

**Local by default.** The default provider is on-host Ollama (`embeddinggemma`,
768-dim): memory text never leaves the machine. An OpenAI-compatible off-host
provider (`NEOBRAIN_EMBED_PROVIDER=litellm`) is opt-in. Vectors are keyed by
`provider:model`, so switching providers re-embeds automatically — no
migration. `neobrain embed` backfills idempotently (hash-keyed, prunes vectors
of deleted atoms).

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
  cacheable system prefix; push-only, so it is never rated.
- **Per-turn recall** — the user's message is recalled against the mind and a
  compact rank-ordered index is injected (top 10 one-liners + the #1 hit's
  text). Curated types (preference/lesson/decision) auto-inject on ordinary
  turns; memory-intent phrasing escalates to deep recall.
- **In-turn rating** — the recall block asks the agent to rate, in-turn, only
  the memories it actually used, via `memory_rate(id, useful | noise)`; unused
  entries stay unrated, with no blocking gate and no cross-session state (the
  former global "unrated protocol" was removed 2026-10-08 — SPEC §5).
  `memory_open(id)` still posts an automatic `used` signal.
- **Compaction hook** — the directives block is also injected into the
  summary request, so rules survive a compact; once-per-session gates reset
  so persona + wake-up re-inject next turn.
- **Pull tools** — `memory_store(text, type?, hubs?, source?, label?, dedupe?)`
  (records the origin session), `memory_open(id)`, `memory_search(query)`,
  `memory_rate(id, useful | noise)`. `memory_store` is for one durable fact
  (a decision, preference, correction, or lesson). The tool description tells
  the agent not to store chatter or status ("ok", "thanks", "done"); the
  server enforces the same rule and the tool reports a rejection as
  `not stored` instead of a bare HTTP error.
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
neobrain service install            # daemon under launchd/systemd (or `neobrain start` for a terminal run)
neobrain ui                         # print and open the Observatory URL
```

Dashboard defaults to `http://127.0.0.1:9192` (`NEOBRAIN_API`); the OpenAPI
schema is at `/api/schema`.

**Run it as a service** — `neobrain start` in a terminal is an unsupervised
process: it can't be restarted from the Settings tab and doesn't survive a
crash or reboot. Put it under the OS service manager instead (launchd
LaunchAgent on macOS, `systemd --user` on Linux):

```bash
neobrain service install            # start at login, restart on crash
neobrain service status
neobrain service restart            # same as the Settings tab's Restart button
neobrain service uninstall
```

On macOS logs go to `<data dir>/daemon.log`; on Linux, `journalctl --user -u neobrain`.
Stop any hand-started daemon before `install` (it refuses if the port is taken).

**Identity** — `neobrain init` seeds `IDENTITY.md` and `SOUL.md` in the workspace
as placeholders (parenthesised values, which the parser ignores). Edit them from
the Identity tab, or leave them: the weekly reflection fills a placeholder in
from what it observes. Existing files are never overwritten. `USER.md` and
`AGENTS.md` are yours to write; they are read but not seeded.

**Infra** — the Infra tab lists `INFRA_<topic>.md` files in the workspace root
(the map of the machine: decisions, lessons, open questions). Nothing generates
them; write one and the next ingest picks it up.

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
| `remember` | store a memory (`--type --hubs --link kind:target --label --dedupe`); filler exits 2 |
| `recall` | recall a relevant subgraph (`--limit --json --lexical`) |
| `wakeup` | compact wake-up pack (identity + open loops + recent) |
| `pin` | pin/unpin an atom as a standing directive (`--off`) |
| `directives` | show the standing directives re-injected every turn |
| `embed` | backfill local embeddings (idempotent; `--force --limit`) |
| `dreams` | run a dream pass (`--phase light\|rem\|deep\|all`, `--replay YYYY-MM-DD`) |
| `reflect` | run the weekly reflection pass (re-authors the persona docs) |
| `forget` | delete a memory by id |
| `maintain source-cleanup` | dry-run (default) or delete memories whose `source` starts with a prefix (default `icm:`); needs `--apply --confirm` |
| `link` | wire two memories (`kind`: supersedes, derived-from, caused-by, consolidates, about) |
| `consolidate` | dream pass on demand (merge hubs, promote patterns) |
| `feedback` | record a quality signal (`used\|useful\|noise`) |

## API

Read-mostly JSON; groups worth knowing (full schema at `/api/schema`):

- `/api/mind/*` — `stats`, `graph`, `atom/{id}`, `activity`, `memories`,
  `recall`, `wakeup`, `directives`, `pin`, `remember`, `feedback`,
  `consolidate`, `dreams`, `reader`, `import`, and
  `maintenance/source-cleanup` (dry-run only; see
  [`docs/MEMORY.md`](./docs/MEMORY.md)). `POST /api/mind/remember`
  stores one atom for any client (no session required). Empty text and
  generic filler are refused with **422** and
  `{"stored": false, "rejected": true, "code": "empty"|"filler", "reason"}`;
  nothing is written. Disable with `NEOBRAIN_REMEMBER_QUALITY=0`. See
  [Memory writes](#memory-writes).
- `/api/debug/*` — `requests` (rolling trace of every `/api` call),
  `injections` (plugin lane pushes, GET/POST), `logs` (on-demand journal tail),
  `overview` (rank states, mind ops, feedback, ingest runs, life phases,
  daemon status, self/soul trace, config).
- `/api/settings` — `GET` the effective settings + section metadata, `PUT`
  overrides, `POST /models` (list a provider's models), `/test` (one tiny
  completion), `/restart` (restart the daemon).
- Plus `/api/health`, `/api/stats`, `/api/events`, `/api/sessions`,
  `/api/search`, `/api/docs`, `/api/ingest/run`.

## Memory writes

`mind.remember` (and therefore the API, the CLI, and every adapter) refuses a
write that is not a memory, before anything is stored:

- **empty** — blank text. Always refused.
- **filler** — generic conversation chatter ("ok", "thanks", "got it",
  "done", "sounds good"), a string of punctuation, or text shorter than
  `NEOBRAIN_REMEMBER_MIN_CHARS` significant characters (letters or digits;
  default 3).

A refusal raises `RememberRejected` (`code`, `reason`). The HTTP route turns
that into 422 with a stable JSON body; the CLI prints the same body and exits
2. The store, including the ops log, is unchanged.

The match is on the **whole** text, so a real sentence that merely starts with
an acknowledgement ("ok to ship the fix once CI passes") is stored, and short
durable facts ("Use tabs", "API key rotated") pass the default length. The
filler list is an explicit, English-shaped chatter list — not a language
detector and not a blocklist of names, paths, or hosts.

`NEOBRAIN_REMEMBER_QUALITY=0` stores filler anyway (empty text still fails).
Both knobs are also on the Settings tab (`remember_quality`,
`remember_min_chars`) and apply live. Callers that must not crash on a
refusal — the life loop and the dream/reflect passes — skip that one write and
continue.

## Testing

```bash
.venv/bin/python -m pytest -q
```

Ranking
correctness is covered directly (`test_recall_rank.py`, `test_rank_*.py`).
There is no published retrieval benchmark (recall@k / MRR) yet — until one
exists, treat α as a starting point and tune it on your own store
(`recall --lexical` vs. hybrid makes the comparison by hand).

## Status

Active development. Sprints S0–S7 are done (core mind, Observatory, plugin,
rank, life loop, API/CLI wiring, cutover — see `SPEC.md` §14 for the board
and [`docs/CUTOVER.md`](./docs/CUTOVER.md) for the migration). S8 (npm
launcher + PyInstaller packaging) is pending, so there is no published
package yet — install from this repo.

## License

[MIT](./LICENSE)
