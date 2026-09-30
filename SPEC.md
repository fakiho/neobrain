# neoBrain — SPEC v1.0 (frozen 2026-09-30)

> A simulated brain for AI agents: memory, rank-based forgetting, dreams and a
> self-authored soul, running as a local daemon with a live dashboard.
> The user watches; the agent becomes.

## 1. Vision

Give AI coding agents their own mental life: durable memory they can recall,
an economy that forgets cheaply, nightly dreams that consolidate, and a
character they author themselves — with zero user interaction. Public-first:
`npm install neobrain`.

## 2. Brand (LOCKED — m_5e37bc7d0f54)

- Product: **neoBrain** · npm package: `neobrain` (verified free)
- Repo: `github.com/alionix/neobrain` (handles on GitHub are taken; org publish makes that irrelevant)
- Family: **alionix** → **neohive** (agents collaborate) + **neoBrain** (agents remember)
- CLI: `neobrain init | start | ui | remember | recall | dreams | …`

## 3. Architecture

```
 YOU ──coding──▶ OpenCode ──thin plugin───┐
  │                                       ▼
  └────watches──── Observatory ◀── API ┌──────────────────────────┐
                                       │   neoBrain daemon        │
                                       │  LIFE LOOP (in-process)  │
                                       │  perceive→reflect→act→   │
                                       │  rest                    │
                                       │   ├ BRAIN                │
                                       │   │  memory  atoms+recall│
                                       │   │  rank    feedback+   │
                                       │   │            counters  │
                                       │   │  forgetting: by rank │
                                       │   │  dreams: night replay│
                                       │   │  soul: self-authored │
                                       │   └ own runtime → LLM API│
                                       └──────────────────────────┘
```

One repo = one source of truth. Deployed artifacts elsewhere (plugin symlink,
optional systemd unit) are dumb install outputs of `neobrain init`.

## 4. The organs (LOCKED)

### 4.1 Memory
Atoms + hubs + typed edges in SQLite. Hybrid recall (BM25 + cosine vectors),
**top-K by rank within a token budget** — never load everything. Atoms carry
ids, hubs, links, provenance.

### 4.2 Rank (deterministic — the model has NO discretion)
Score inputs: model feedback (`used | useful | noise` per served memory),
exposure counters (times served, times interacted), recency decay, hub
connectivity. Replaces the timeline's ±10 % feedback multiplier.

**Form (S4 review, 2026-09-30):** `quality = clamp01(feedback · decay · conn)`
with `decay = decay_floor + (1-decay_floor)·recency` and
`conn = conn_floor + (1-conn_floor)·connectivity`. Three *bounded* factors: the
floors keep age and graph isolation from being fatal on their own, but feedback
is the only factor that can reach zero — so a flood of `noise` forgets even a
fresh, well-connected memory, which is what makes the high-exposure rule below
reachable. (An additive blend cannot: recency/connectivity then act as
irreducible floors — verified numerically and fixed before commit.) Exposure
(`served + 3·interacted`) is the second axis and never raises quality. Knobs:
`rank_decay_floor`, `rank_conn_floor`, `rank_half_life_days`, `rank_prior_n`,
`rank_ignore_below`, `rank_archive_exposure` in `config.py`.

### 4.3 Forgetting (rank-based only)
- low rank + low exposure → **ignored forever**: never surfaced, zero cost, never deleted
- low rank + **high** exposure → **archived**: unretrievable but recoverable (archive ≠ delete)
- high rank → served
- Dreams never decide retention. `timeline forget` semantics preserved for manual/ops use.

### 4.4 Dreams
Nightly replay phases (light / rem / deep, ported verbatim from
`timeline/bin/dream.sh` prompts): consolidate top-ranked atoms into compressed
summaries, build associations, write the diary (`DREAMS.md`). No retention power.

### 4.5 Soul
Authored **exclusively** by the agent's own night phases from his own
experiences — zero user interaction. `SOUL.md` / `IDENTITY.md` are generated
projections of the preference/gene state (buildable, inspectable, diffable).
**Red lines are immutable**: safety and spend rules never evolve.

## 5. Enforcement model (LOCKED — m_216def778232)

| Behavior | Class | Mechanism |
|---|---|---|
| Rules injected at session start | deterministic | session bootstrap injection |
| Awareness deep into long sessions | deterministic | compaction re-inject + on-change delta push |
| Recall happens | deterministic | results injected into context — not the agent's decision |
| Feedback given | protocol | served atoms marked `unrated`; next mind call blocked until rated; **auto-fallback**: pending clears after timeout, counting as exposure with no verdict |
| Store via mind tool | protocol + ergonomics | single mind tool as path of least resistance; ingest captures the session regardless |

Honest limit: pure tool-choice compliance can never be 100 % with an LLM in
the loop — the system is designed so the critical parts do not depend on it.

## 6. Life loop

In-process state machine inside the daemon (cadence in code + DB, **not** OS
timers): **perceive** (ingest sources + health checks; failures become atoms)
→ **reflect** (consolidate) → **act** (urge queue: opinions, proactivity,
tasks) → **rest** (dream phases nightly). systemd tick = optional Linux extra.

## 7. Runtime (LOCKED — m_07143f9c901d, m_56b788795ffd)

The brain's own cognition runs on **native LLM calls** (OpenAI-compatible
endpoints; LiteLLM gateway in-house) — OpenCode is never used for the brain's
own thoughts. Model tiering: cheap model for perceive/reflect, stronger for
act/dream.

## 8. OpenCode integration

Thin plugin `adapters/opencode/neoBrain-memory` (port of
`~/.opencode/plugins/timeline-memory/index.ts`): session wakeup injection,
per-turn recall lane, `memory_open/search/rate` tools, unrated protocol (§5).
No logic beyond protocol. Other CLIs later (pi/omp/hermes noted; designated
fork path: pi family — m_bf4b1d06e38e).

## 9. Distribution (LOCKED — m_42f1427ef9ad, m_2500158b4cc4)

- npm thin launcher `neobrain` → **PyInstaller per-platform binaries** as
  `optionalDependencies` (`darwin/linux × x64/arm64`; musl = fast-follow;
  baseline x64 variant). The proven codex/opencode/droid pattern — no
  postinstall scripts (blocked by pnpm 10/Bun), no host Python (PEP 668).
- `neobrain init` — detects opencode, installs plugin, generates persona, inits DB
- `neobrain start` — daemon: life loop + API + dashboard on localhost
- **Local-first defaults**: local Ollama embeddings; BYOK OpenAI-compatible
  endpoint; nothing leaves the host unless the user opts in.

## 10. Language & quality (LOCKED — m_56b788795ffd)

- **Core: Python 3.12** — chosen over TS rewrite: audit verified ~70 % portable
  as-is and the user's tiebreaker is time/cost to completion.
- Type discipline: pyright (strict for new modules), **pydantic at every
  boundary** (API payloads, LLM responses, config).
- Plugin + dashboard: TypeScript (React/Vite), as today.
- **Reference oracle**: the old timeline app keeps running; parity tests
  (`tests/parity/`) must reproduce scorer/recall/consolidation outputs.
- One authoritative schema module; `PRAGMA user_version` migrations (the old
  repo had none — audit finding).

## 11. Security & privacy

Private data stays on host by default; local Ollama embeddings by default;
secrets only in `.env` (gitignored); red lines immutable; no destructive
commands; no sudo in any phase.

## 12. Open questions (resolve during sprints, do not block S1–S3)

1. Soul update cadence + max change per update
2. Top-K budget size and rank weight tuning (empirical)
3. Urge policy: what may `act` do alone vs ask
4. Archive retention: TTL or forever
5. May the agent initiate contact with the user, or only respond

## 13. Decision log (Timeline mind atoms)

| Decision | Atom |
|---|---|
| Reframe: brain, not ops agent | `m_61b7eda25dd7` |
| Forgetting v1 (rank-based, ignored-not-deleted) | `m_8432622ff6a9` (superseded) |
| Forgetting v2 (feedback + counters, eviction rule) | `m_2d90e7f99e54` |
| Runtime: own runtime, OpenCode = hands, no fork | `m_07143f9c901d` |
| Coding CLI selection (opencode stays; pi = fork path) | `m_bf4b1d06e38e` |
| Distribution: public npm, daemon, local-first | `m_42f1427ef9ad` (+ research `m_2500158b4cc4`) |
| TS lock | `m_209f2bc74183` (superseded) |
| **Language FINAL: Python core** | `m_56b788795ffd` |
| Enforcement upgrade | `m_216def778232` |
| Port audit | `m_84450cbf7c6d` |
| Name locked | `m_5e37bc7d0f54` |

## 14. Sprint plan

| Sprint | Scope | Status |
|---|---|---|
| S0 | Repo + SPEC + skeleton | done at freeze |
| S1 | Core mind port: `mind.py`, `embeddings.py`, `db.py`, `config.py`, authoritative schema | done (validated) |
| S2 | Observatory web port (drop dead code) | done (build clean, tsc clean) |
| S3 | opencode plugin port + unrated protocol | done (tsc clean + mocked-fetch smoke) |
| S4 | `rank.py` + exposure counters + eviction/archive job | done (calibrated in review) |
| S5 | Life loop + native LLM runtime (`life.py`, `runtime.py`) | done (validated) |
| S6 | `api.py` + `cli.py` + `ingest/` wiring | pending |
| S7 | Parity tests vs oracle + DB migration from timeline.db + cutover | pending |
| S8 | npm launcher + PyInstaller packaging | pending |
