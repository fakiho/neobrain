# CUTOVER — timeline → neoBrain (2026-09-30)

Reversible, step-by-step. Every step names its rollback. No deletions: the old
container, plugin and units are stopped/disabled, not removed.

## Pre-state (verified 2026-09-30 ~20:30 CEST)

| Component | Current |
|---|---|
| Brain store | `/home/sparo/timeline/data/timeline.db` (112 MB, 326 atoms) |
| API/dashboard | docker container `timeline` (`timeline:local`), `0.0.0.0:9192`, public via NPM host #18 → `timeline.alionix.com` |
| Nightly work | 4 **user** timers: `opencode-dream-{light,rem,deep}.timer`, `opencode-reflect.timer` (light fires 22:32 tonight) |
| Memory plugin | `~/.opencode/plugins/timeline-memory/` (real directory) |
| Cron | none for timeline (verified) |

## Steps

1. **Backup** (before anything): consistent SQLite snapshot + `.env` + unit files
   + docker inspect → `/home/sparo/backups/timeline-pre-cutover-<ts>/`.
2. **Disable old scheduling** (rollback: `systemctl --user enable --now …`):
   `systemctl --user disable --now opencode-dream-light.timer opencode-dream-rem.timer opencode-dream-deep.timer opencode-reflect.timer`
3. **Stop old container** (rollback: `docker start timeline`):
   `docker stop timeline && docker update --restart=no timeline`
4. **Migrate data**: snapshot → `/home/sparo/neobrain/data/neobrain.db`, then
   `db.init_db` (applies legacy DDL + v2 migration: `m_rank` backfill,
   `user_version` 0→2). Verify atom/feedback/edge counts match the snapshot.
5. **Config**: `/home/sparo/neobrain/.env` — workspace, data dir, LLM key,
   bind `0.0.0.0:9192`, life loop on.
6. **Daemon**: user unit `~/.config/systemd/user/neobrain.service`
   (mirrors the existing user-timer pattern; rollback: `disable --now`).
   Restart=on-failure; starts the API + dashboard + in-process life loop.
7. **Verify**: `/api/health`, dashboard 200, life phase events, public URL.
8. **Plugin swap** (rollback: move the old directory back):
   `mv ~/.opencode/plugins/timeline-memory ~/.opencode/plugins/.archive-timeline-memory`
   `ln -s /home/sparo/neobrain/adapters/opencode/neoBrain-memory ~/.opencode/plugins/neobrain-memory`
   **Activation requires an OpenCode server restart** (`sudo systemctl restart
   opencode.service` — never `opencode service restart`, see INFRA_MAP §2) which
   ends the current agent session. Until then the running server keeps the old
   plugin; both point at `:9192` so the brain is served either way.
9. **Docs**: `INFRA_TIMELINE.md` (architecture), `INFRA_MAP.md` (entry), memory
   section of `AGENTS.md` (`timeline …` → `neobrain …`), daily note, atoms.

## Rollback (any point)

```bash
systemctl --user disable --now neobrain.service
docker update --restart=always timeline && docker start timeline
systemctl --user enable --now opencode-dream-light.timer opencode-dream-rem.timer \
  opencode-dream-deep.timer opencode-reflect.timer
mv ~/.opencode/plugins/neobrain-memory /tmp/ 2>/dev/null
mv ~/.opencode/plugins/.archive-timeline-memory ~/.opencode/plugins/timeline-memory
```

The old DB is never modified: neoBrain reads a *copy*, so rollback loses only
memories written after the cutover.
