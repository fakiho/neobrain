# Plan: memory health fixes (forgetting + agent saves)

Date: 2026-10-16
Status: shipped on `main` (2026-10-16). The "What the review found" section below is the snapshot that started the work, not the current store. What landed is in [What shipped](#what-shipped); what did not is in [Still open](#still-open).
Live DB: the configured neoBrain database (read-only inspection on this machine). This work did not modify it.

## Portability requirement

neoBrain is a library, not a personal-app implementation. All fixes must work on
Linux and macOS, and must not depend on this machine's paths, LaunchAgent,
`launchctl`, shell layout, or OpenCode-specific behavior in the core package.

- Core forgetting, ranking, validation, and cleanup logic belongs in the
  platform-neutral Python library and must use configured paths and existing DB
  abstractions.
- The life-loop integration must use the existing portable daemon lifecycle;
  service supervision differences remain outside the memory logic.
- OpenCode changes belong only in the optional adapter under
  `adapters/opencode/`; other clients must still be able to use the library's
  API/tools without that adapter.
- Tests must run on both Linux and macOS, use temporary/configured databases,
  and avoid hard-coded home directories, timestamps, or service commands.
- The ICM cleanup must be an API/library operation or portable maintenance
  command, not a machine-specific SQLite command or manual path.

## What the review found

1,125 atoms, 23 hubs, 1,351 edges. 1,065 atoms (94%) are the ICM migration
(`source LIKE 'icm:%'`) and are noise. They are excluded from this plan's
fixes and handled separately by the cleanup step at the end.

### Forgetting is dead code

- `rank.run_eviction` is called only from `tests/test_rank_engine.py` and
  `tests/test_rank_calibration.py`. No caller in `src/`.
- `rank.recompute` (the full recompute that applies recency decay and
  connectivity) is also called only from tests. The only live path is
  `rank.on_feedback`, which refreshes **one** atom, and only for `useful` /
  `noise` / `used`.
- `unused` is deliberately skipped in `mind.feedback` (`src/neobrain/mind.py`
  ~line 835): it never touches `m_rank`.
- Result in the live DB: all 1,098–1,103 ranked atoms are `state='active'`.
  Quality sits between 0.282 and 0.5 (average 0.497). The forget threshold is
  `rank_ignore_below = 0.15` (`config.py`), so even a perfect eviction run
  today would demote **zero** atoms.
- Recall itself is fine: `mind.recall` already excludes `state <> 'active'`
  (`mind.py` ~line 1126). The gap is upstream — nothing ever changes the state.
- Eviction only demotes (`ignored` / `archived`). It never deletes. That is by
  design (`rank.py` header) and stays that way; deletion is a separate,
  explicit cleanup.

### Ratings barely work

- 216 feedback rows, all from `source='plugin'`: 198 `unused`, 18 `used`.
  **Zero** `useful`, **zero** `noise`.
- `memory_rate` (plugin tool, posts `source='agent'`) has never been called.
  The only prompt to rate is one line inside the recall block
  (`adapters/opencode/neoBrain-memory/index.ts` line 169), and nothing enforces
  it. The old blocking "unrated protocol" was removed on purpose because it
  forced near-constant `useful` ratings — do not bring it back.
- `used` is only recorded by `memory_open`, which agents rarely call because
  the recall block already inlines the top hit's text.
- `unused` is recorded (the turn-boundary inference works) but has no effect
  on rank, so the one signal we do collect is thrown away.

### Agents are not saving memories — investigated

Evidence, not a guess:

- `m_ops` where `op='store'`: 1,083 total, but 1,071 of them are on
  2026-10-14 (the ICM import day). Since then: 4 on the 15th, 5 on the 16th,
  and every one of those is `life:act` or `dream`, i.e. the daemon writing to
  itself. **Zero stores from an agent session.**
- Only 8 atoms have a `session_id`, and none of them is an OpenCode session:
  `dream`, `reflect`, `cli`. The plugin's whole point (`memory_store` posts
  `session` = the live sessionID) has never produced a row.
- The plugin is loaded and healthy: 200 injection reports
  (171 directives, 11 wakeup, 11 persona, 7 recall) and 127 `[neobrain-memory]`
  log lines. So this is not "the plugin is down".
- `memory_store` is registered and the daemon endpoint works
  (`POST /api/mind/remember`, `api.py` line 497). The pipe is fine; nothing
  calls it.

Root cause: **nothing tells the agent to save.** The wake-up pack mentions
`neobrain remember` (a CLI the agent does not have), the tool description says
what `memory_store` does but never when to use it, and no `AGENTS.md` or
`opencode.jsonc` instruction mentions it. Saving is a pure judgement call the
model never makes. (This session is an example: several durable facts were
established and none were stored.)

Secondary: even if agents did save, there is no quality gate on ingest, which
is exactly how the ICM dump got in. A fresh start without a gate just refills
with filler.

## Fix 1 — wire forgetting into the portable life loop

Core files: `src/neobrain/life.py`, `src/neobrain/rank.py`,
`src/neobrain/mind.py`, and `src/neobrain/config.py`.

Use the existing due-based life loop shared by Linux and macOS, not LaunchAgent,
systemd, cron, or platform-specific service hooks. In the nightly rest/dream
boundary, or another existing portable phase:

Two calls, in this order, both best-effort like the other phases:

1. `rank.recompute(conn)` — refreshes every atom's quality from feedback,
   recency and connectivity. Without this, decay never happens.
2. `rank.run_eviction(conn)` — demotes anything under `rank_ignore_below`.

Also record the result as a `life: evict` event so the Observatory shows it,
same shape as the other phase events.

Record the result with the existing portable event abstraction so the
Observatory can show it. Do not add OS timers or service-manager calls.

Tests: extend `tests/test_life_loop.py` (or the life rating-watch test) using
temporary/configured databases and injected clock values. Assert a low-quality
atom is demoted when the phase runs, and that a 0.5-prior atom is not on both
Linux and macOS.

### Make the threshold reachable

`rank_prior_n = 10` means one `noise` only moves quality from 0.5 to ~0.45.
Reaching 0.15 needs roughly 24 `noise` ratings on one atom, which will never
happen. Two changes, both config-driven, both covered by
`tests/test_rank_calibration.py`:

- Count `unused` in the rank. It is a real signal (the plugin collects it) and
  is currently discarded. Weight it well below `noise` — proposed
  `unused = 0.3` in `SIGNAL_VALUE`, and remove the `signal != "unused"` skip in
  `mind.feedback`. Prefer a single-atom incremental update on feedback; the
  full graph recompute still runs once per night.
- Lower `rank_prior_n` from 10 to 4 so a handful of negative signals can
  actually cross 0.15, while one stray `noise` still cannot. Keep both values
  configurable in `Settings`, not hard-coded.

Do not lower `rank_ignore_below`. The problem is the score never moves, not
the threshold.

## Fix 2 — make agents save

The tool works; the instruction is missing. Three small edits, no new
machinery:

1. `adapters/opencode/neoBrain-memory/index.ts` `renderPack`: replace the
   `neobrain remember` CLI hint with `memory_store`. The agent has the tool,
   not the CLI.
2. Same file, `memory_store` tool description: state **when** — a decision, a
   correction, a preference, or a lesson that would still matter in a later
   session. Explicitly exclude status chatter, questions, and tool-output
   summaries (the exact shapes that polluted the ICM import).
3. Add one standing line to the optional adapter's injected directives so it
   survives compaction: "Save a durable fact with memory_store; do not save
   chatter." This must not be required by the core library; other adapters
   need an equivalent integration point or can use the API directly.

### Guard the write path in the core library

`mind.remember` (`src/neobrain/mind.py` ~line 703) should reject, with a clear
portable error, text that is a question, under ~40 characters, or matches the
filler patterns ("what would you like", "the tool outputs show", "let me know").
Keep the validator small, deterministic and configurable where appropriate; do
not encode personal names, paths or OpenCode-specific assumptions in it.

Return the rejection through the API response so every client sees why. Add
unit tests using temporary DBs and platform-neutral strings. This prevents a
future migration/import from refilling the store with conversation filler.

## Fix 3 — ratings

Do not reintroduce the blocking unrated protocol. Instead:

- Record `used` for the atoms actually pushed into the prompt, not only for
  `memory_open`. The plugin already knows the pushed ids (`pushedTurn`); post
  `used` for the top hit it inlines, since that text was genuinely shown.
- Keep the `memory_rate` hint, but move a shorter version into the tool result
  of `memory_search` and `memory_open`, where the agent is already holding an
  id. The current hint is buried in a block the model treats as reference.
- Add a debug counter: log when `memory_rate` is called and when it is not, so
  the next review can see the difference instead of inferring it from an empty
  table.

## Fix 4 — portable ICM cleanup (after the above)

Not part of the code fixes. Once forgetting and the write guard exist:

- Add a portable, dry-run-first maintenance operation that selects atoms by
  provenance (`source LIKE 'icm:%'`), reports counts/edges/hubs, and requires
  explicit destructive confirmation.
- Back up through the configured database/backup abstraction, not a hard-coded
  path or shell command.
- Delete selected atoms and connected edges transactionally, then remove hubs
  left empty. Keep everything else (lessons, preferences, dreams,
  SOUL/IDENTITY).
- Test rollback and repeatability on temporary SQLite databases.

This is a one-off maintenance operation, not a change to `forget()`, and it
must work from the library/portable CLI on both Linux and macOS.

## What shipped

Hashes are the commits on `main` that landed each piece. Tests (`2d8e068`, `90bea82`, `aca435e`) landed alongside the implementation commits (`27955d9`, `10f83f3`, `35990ef`, `150916b`, `7df8c38`) across those two cherry-pick batches.

### Fix 1 — forgetting in the life loop: shipped, narrower than proposed

`aeca426` (`feat(rank): run forgetting from the portable life loop`).

Shipped:

- `LifeLoop.tick` calls `rank.recompute` then `rank.run_eviction` when the `evict` phase is due (`rank_interval_minutes`, default one day). It runs in quiet hours too, records a `life: evict` event, and never deletes.
- `rank_ignore_below` stayed at `0.15`.

Not shipped, on purpose. The commit message says so, and the code matches:

- `unused` is still absent from `SIGNAL_VALUE` and `mind.feedback` still skips it for rank. Recall already applies it as a small capped drag; folding it into quality would let a pile of inferred non-answers forget a memory.
- `rank_prior_n` stayed at `10`. One stray `noise` must not cross the threshold.

### Fix 2 — agents save, and the write path refuses filler: shipped

`db8e089` (`feat(memory): reject filler writes in mind.remember with a portable error`), then `ce0f3b7` (`fix(memory): write gate refuses full-sentence agent filler`).

Shipped:

- `mind.remember` raises `RememberRejected` before any write for empty text and for filler. HTTP returns 422 `{"stored": false, "rejected": true, "code", "reason"}`; the CLI exits 2. `NEOBRAIN_REMEMBER_QUALITY` and `NEOBRAIN_REMEMBER_MIN_CHARS` configure it. The minimum is 3 significant characters, not ~40.
- The gate matches the whole text, not a substring. Besides a filler vocabulary it refuses a few full-sentence shapes (closers such as "let me know if you need anything else", "what would you like to do next", and tool-status narration). A real memory that merely contains those words is kept.
- The OpenCode adapter (optional client) tells the agent to use `memory_store`, states when to call it and when not, and surfaces a rejection as `memory_store: not stored (code) — reason`. `renderPack` carries the standing line "Use memory_store only for a durable decision, preference, or lesson — never for chatter or status."

### Fix 3 — ratings: partly shipped

`c8e5b61` (`adapter: record used for the inlined recall hit, add one-line rating hint`).

Shipped:

- The adapter posts `used` for the atom whose text the recall block actually inlines (the top hit, once per turn), and keeps that id out of the `unused` inference. `memory_open` still posts `used` too.
- The recall block carries one advisory `memory_rate` line. It is a hint, not a gate. The blocking unrated protocol was not brought back.
- `reportFeedback` already logs each feedback post (`feedback → <signal> <id>`) and a failed post. The turn boundary logs how many pushed atoms were left `unused`.

Not shipped:

- The hint was not moved into the `memory_search` or `memory_open` tool result. `memory_search` returns the ranked list only.
- There is no separate counter that logs a turn where `memory_rate` was not called. Absence is still inferred from the feedback table.

### Fix 4 — portable provenance cleanup: the command shipped; the live run did not

`c5e92a0` (`feat(maintenance): dry-run-first provenance cleanup for icm: imports`).

Shipped: `neobrain maintain source-cleanup` plans read-only, requires a confirm token over the exact matched set, backs up with SQLite's online backup API, and deletes in one transaction. HTTP exposes only the dry-run. Documented in `docs/MEMORY.md`.

Not run: nobody has applied it to the live store. That still needs explicit approval. See [Still open](#still-open).

### Also shipped (not one of the four fixes)

These came out of the same health pass. They are recorded so the status line is not the only place that says they exist.

- **History.** `dcecb83` adds `m_history` (schema v6): `add` and `delete` in the same transaction as the change. `0b8688e` documents the gaps. `link` events came with the explicit-link work below.
- **Eval.** `a5e717e` adds the deterministic golden-set eval (`recall@k`, MRR, junk rate). No model, no network.
- **Supersession hiding.** `bfcb8d4` hides superseded atoms by default, with `--include-superseded` to opt back in. `150916b` narrows that to caller-stated edges only: a guessed `supersedes` edge, or one with no provenance, does not hide. `90bea82` is the test commit for that filter, and `aca435e` covers the `remember(links=…)` path, which stays visible.
- **Explicit links.** `27955d9` adds `m_edge_meta` (schema v7) and `mind.link(..., explicit=True)`. `10f83f3` keeps the edge and its meta in one transaction and deletes the meta wherever the edge is deleted. `35990ef` makes `remember(supersedes=…)` one transaction: a bad target stores nothing. `2d8e068` is the test commit for that work. The adapter's `memory_link` and `memory_store` `supersedes` are optional-client surface.
- **Upgrade.** `7df8c38` marks every `supersedes` edge that already exists at v7 migration time `explicit=1`, with `actor='migration:v7'`. That actor is a label for the upgrade, not a claim that a caller stated the link. Edges created after the upgrade are not backfilled, so a new guess still does not hide.

## Still open

- **Live ICM cleanup has not been run.** The command exists (`c5e92a0`) and is dry-run by default. Applying it deletes the `icm:` atoms and needs explicit approval. Do not run it as part of reading this plan.
- **Daily eviction is not yet validated against live data.** The life loop calls it (`aeca426`), and the tests use temporary databases. Nobody has checked a real `life: evict` pass against this machine's store. Because `unused` is not a rank input and `rank_prior_n` stayed at 10, a pass today still may demote nothing — the review's threshold problem was deliberately not "fixed" by counting inferred non-answers.
- **The existing guessed links in the live store still hide; the count was not re-verified.** A single live-store review on 2026-10-16 reported 6 such links. That number is not re-verified here, and no code or test asserts it. The mechanism is the v7 migration: `src/neobrain/schema.py` `migrate`, the `v < 7` branch, inserts one `m_edge_meta` row per existing `supersedes` edge with `explicit=1` and `actor='migration:v7'`. That marks every existing `supersedes` edge explicit, including ones the keyword guesser wrote before the upgrade, so those links keep hiding after upgrade. A new guess does not get that treatment. Clearing one takes a manual re-link (delete the edge, or state the correct replacement) or the ICM cleanup, if the atom is in that set. This plan did not inspect the live database again.

## Order of work

1. Fix 1: portable life-loop integration + rank calibration, with tests.
2. Fix 2: core save validation, then the optional OpenCode adapter guidance,
   with tests for both layers.
3. Fix 3: rating telemetry and adapter usage signal.
4. Fix 4: dry-run, backup, transactional ICM cleanup.

After each step, run the platform-neutral test suite. Run adapter typecheck only
when the adapter changes. Do not touch or delete the live ICM data until step 4
is explicitly approved.

## mem0 inspiration (adapt, do not copy)

Reviewed against mem0's current docs, paper, OSS history schema, and public
issue discussion. Useful ideas for neoBrain are:

- Keep memory changes append-only. Record atom history (`event`, old/new text,
  actor, reason, timestamp) in the same transaction as the state change. This
  is portable and does not require an LLM.
- Handle contradictions by supersession, not silent overwrite. Preserve the old
  atom, link it to the newer one, and filter superseded atoms at recall. This
  fits neoBrain's existing edge model better than destructive updates.
- Apply scope and metadata filters in SQL before lexical/vector ranking. Do not
  introduce mem0's hosted user/agent/app/run schema unless neoBrain has a real
  multi-tenant need.
- Evaluate with a deterministic golden set: fixed atoms, expected recall IDs,
  recall@k/MRR, and a junk-rate metric. Do not depend on an LLM judge.
- Prevent junk at write time. Record provenance, reject recalled content being
  re-saved, and use explicit validation rather than hoping extraction quality
  improves. mem0's public audit showing ~98% junk is a warning, not a pattern to
  reproduce.

Do not copy mem0's prompts, retrieval implementation, hosted platform schema, or
LLM-controlled ADD/UPDATE/DELETE/NOOP flow. neoBrain's retention decisions stay
deterministic.

## Out of scope

- Changing the eviction model to delete. Demotion stays the mechanism;
  deletion stays manual.
- Re-introducing a blocking rating gate.
- Touching the dream or consolidate passes beyond adding the portable rank
  maintenance call.
- Assuming LaunchAgent, systemd, cron, macOS paths, or Linux paths in core
  behavior.
