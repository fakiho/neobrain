# Memory model and maintenance

How neoBrain stores memory, and how to audit or remove a batch of memories
that came from somewhere else (today: an import whose `source` starts with
`icm:`). This describes the code as it is; it does not change the frozen
[SPEC](../SPEC.md).

## The model

Three core tables, all in the single SQLite store (`neobrain.db`, schema in
`src/neobrain/schema.py`):

| Table | What it holds |
|---|---|
| `m_atoms` | One row per memory: id, label, type, text, `source`, tags, weight, hub, content hash, origin `session_id`. |
| `m_hubs` | Named topics a memory can be about (`agent`, `dns`, …). |
| `m_edges` | A typed link between two ids: `about`, `supersedes`, `caused-by`, `derived-from`, `consolidates`. |

Around them:

- `m_rank` — one row per atom (quality, how often it was served, forgetting
  state). Deleted automatically with its atom (`ON DELETE CASCADE`).
- `m_embeddings` — the local vector for hybrid recall. Deleted explicitly on
  forget/cleanup (no foreign key there).
- `m_feedback`, `m_ops` — append-only history of ratings and operations.
  Deleting a memory does **not** delete its history; the rows stay and point
  at an id that no longer exists. This matches `neobrain forget`.
- `m_history` — append-only record of each state change of an atom (`add`,
  `delete`), with the text involved, who did it, and why. See
  [History](#history).

`source` is free text saying where a memory came from: the CLI, a doc path, a
dream pass (`dream:consolidate`), or an import. A memory imported from another
tool is marked with a **namespace prefix**, `icm:` (`icm:import:…`). That
prefix is the only handle the cleanup below uses — it never matches on text.

## Superseded memories

Default recall hides an atom only when an explicit `supersedes` edge points
at it from an *active* atom (an atom with no rank row counts as active).
Edges that already existed when a store upgrades to v7 are marked explicit,
so an upgrade does not un-hide memories that were already hidden. A guessed
edge created after that, or one with no provenance, does not hide. If the
superseding atom is ignored or archived, the older one is shown again. This is
a read-time SQL filter only: nothing is deleted or rewritten. Pass `include_superseded=true`
(API query param), `include_superseded=True` (`mind.recall`) or
`neobrain recall --include-superseded` to get both.

## History

`m_history` is the audit trail of explicit memory writes and deletes. One row
per change (see the known gaps below):
`ts`, `atom_id`, `event` (`add` or `delete`), `old_text`, `new_text`, `actor`
and `reason`.

- **Append-only.** Rows are only ever inserted; nothing updates or removes
  them.
- **Same transaction.** The row is written inside the transaction that makes
  the change, before it commits, so the trail and the store cannot disagree: a
  rolled-back change leaves no history row.
- **Survives deletion.** There is no foreign key to `m_atoms`, so the history
  of a forgotten atom stays readable, including the text it had (`old_text`).
- What is recorded today: `remember` writes `add` (actor = session id, else
  source); `forget` writes `delete` with the old text; a source cleanup writes
  one `delete` per removed atom (actor `maintenance`, reason = the prefix). An
  exact re-store skipped by `dedupe` changes nothing and writes nothing.
- Stores created before schema v6 start with an empty history; nothing is
  backfilled.
- **Not recorded (known gaps).** History covers explicit agent/user writes and
  deletes, not machine-regenerated atoms or rank state:
  - Derived atoms that are rebuilt wholesale on each pass are deleted and
    recreated without history rows: doc-derived atoms on re-import, the
    `dream:consolidate` atoms, and promoted `m_pd_*` atoms dropped on refresh.
    Logging them would write a full copy on every run.
  - Forgetting-state changes (`active` → `ignored`/`archived`) made by the life
    loop live in `m_rank`, not in `m_history`.
  - Text edits and `supersedes` links are not yet recorded; the `event` column
    is free text, so adding them needs no migration.

Read it with `neobrain history <atom_id>` (oldest first, JSON),
`GET /api/mind/history?atom_id=…`, or `mind.history(conn, atom_id)`. All are
read-only.

## Portability

- The store is one SQLite file. Copying the file (or the backup the cleanup
  writes) moves the whole memory with it; there is no server-side state.
- Schema changes are additive `PRAGMA user_version` migrations (`schema.py`).
  Maintenance reads `sqlite_master` and skips tables that are not there, so it
  works on a store from before embeddings/rank existed.
- Nothing here is tied to a machine, a cloud account, or a vector database.
  Embeddings are a blob in the same file.

## What was borrowed, and what was not

The maintenance idea — *inspect a slice of memory, show what removing it
touches, and only then delete it* — is the same shape as memory-maintenance
flows popularised by tools such as mem0 (audit before mutate, scope a cleanup
to one provenance). The implementation is not: no mem0 code, schema, API or
wording is copied. neoBrain's version is ~300 lines over its own three tables,
uses SQLite's own backup API, and refuses to delete without a token computed
from the exact set of rows it would remove.

## Maintenance: provenance-scoped cleanup

```bash
neobrain maintain source-cleanup                 # dry-run, default prefix icm:
neobrain maintain source-cleanup --prefix icm:   # same, explicit
neobrain maintain source-cleanup --apply --confirm delete-N-<hash>
```

**Dry-run is the default and is read-only** — the CLI opens the database with
`mode=ro`, so a dry-run cannot write even if a bug asked it to. It prints:

- how many atoms match, how many would remain, and a sample (id, label, source);
- how many of the matched atoms are pinned directives;
- edges that would be removed, including edges from a *kept* atom into a
  matched one (those links are lost too);
- embedding and rank rows that would go with them;
- feedback/ops rows that would be left dangling (they are kept);
- hubs that would be left with no atoms. They are **not** deleted unless you
  pass `--prune-empty-hubs`.

It also prints a `confirm_token` and the exact command to apply it.

**Applying** needs that token. The token is a hash of the prefix, the prune
flag and the sorted ids of every matched atom, recomputed against the live
store at apply time — so a token from before someone stored another `icm:`
memory is refused and nothing is deleted. There is no `--yes` shortcut.

On apply, in order:

1. **Backup**, unless `--no-backup`. neoBrain had no backup helper, so this one
   is small and local: `sqlite3.Connection.backup()` (the standard library's
   online backup API, which is consistent even while WAL is in use) into
   `<data dir>/backups/neobrain-pre-cleanup-<timestamp>.db`. The copy is then
   checked with `PRAGMA integrity_check` and an atom count against the live
   store; a failed check deletes the copy and aborts before any row is touched.
   This is a best-effort verified copy, not a general backup system — it does
   not rotate old copies, and it cannot back up an in-memory database (those
   must pass `backup=False` explicitly; the CLI cannot, because it always has a
   file).
2. **One transaction** (`BEGIN IMMEDIATE`): re-check the token under the write
   lock, delete the matched edges, embeddings, rank rows and atoms, and — only
   with `--prune-empty-hubs` — hubs that now have no atoms. Any error rolls the
   whole thing back; the cleanup can be retried.
3. One `m_ops` row recording that a cleanup ran.

Matching is a case-sensitive literal prefix (`substr(source,1,n) = ?`), not SQL
`LIKE`. `LIKE` is case-insensitive in SQLite and treats `%` and `_` as
wildcards, which is the wrong behaviour for something that deletes. For the
intended `icm:` prefix the two select the same rows except that `ICM:…` is
left alone. The prefix must end in `:` and be at least two characters, so an
empty or one-letter prefix cannot match half the store.

### What it will not do

- It will not run against a store you did not point it at. The CLI uses
  `NEOBRAIN_DATA_DIR` like every other command and refuses to create a database
  that does not exist yet.
- The HTTP API exposes only the dry-run
  (`GET /api/mind/maintenance/source-cleanup`). The API is unauthenticated and
  allows any origin, so there is intentionally no endpoint that deletes.
- It does not touch memories whose `source` lacks the prefix, and it does not
  touch the event timeline, sessions, or settings.

## Workflow

1. Dry-run and read the counts. Check `pinned_selected` and
   `edges.from_retained_to_selected` — those are the surprising losses.
2. Keep the printed backup path once you apply.
3. Apply with the token. If it says the token is stale, dry-run again; the
   store changed.
4. To undo, stop the daemon and replace `neobrain.db` (and its `-wal`/`-shm`
   sidecars) with the backup file. There is no automatic restore command.

## Evaluation

neoBrain ships a small, deterministic memory-quality eval. It has no model, no
judge and no network: the same code gives the same numbers on any machine.

- **Golden set.** `src/neobrain/eval_data/memory_golden.json` holds 16 atoms
  (preferences, decisions, lessons) with fixed text, and 12 queries that each
  list the atoms that should be found. It is loaded into a fresh temporary
  store with `mind.remember`, then queried with lexical recall
  (`semantic=False`, so no embedding service is needed).
- **recall@k and MRR.** recall@k is the share of expected atoms that appear in
  the top k results (k = 5), averaged over queries. MRR is the mean of
  1 / rank of the first expected atom (0 when it is missing). The test floors
  are recall@5 ≥ 0.8 and MRR ≥ 0.6.
- **Junk rate.** The same file lists conversational filler and short real
  memories. The report counts how much filler the write gate
  (`mind.remember_rejection`) refuses and how many real memories it wrongly
  refuses: `filler_rejected / filler_total`, `real_rejected / real_total`.

Run it:

```
neobrain eval                      # prints the JSON report; uses a throwaway store
pytest tests/test_memory_eval.py   # asserts the floors and the junk gate
```

From Python: `neobrain.eval.run(conn)` on an empty temporary connection returns
`{"recall_at_k", "mrr", "k", "queries", "failures", "junk"}`. `neobrain eval`
never reads or writes your real store.

The idea — audit memory quality with fixed cases — is adapted from published
memory-evaluation practice. No external project's data, prompts or code are
used; the cases here are written for this repository.

### Write gate coverage

Every filler sample in the golden junk set is refused, and none of the real
short memories is. Besides vocabulary matching, the gate refuses a few
full-sentence chatter shapes (closers such as "let me know if you need anything
else", "what would you like to do next", and tool-status narration such as "the
tool outputs show the command succeeded"). These patterns must match the whole
text, so a real memory that merely contains such words is kept.
