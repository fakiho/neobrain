# persona-bootstrap (OpenCode adapter)

Thin OpenCode plugin: injects the agent's persona files into the system prompt at
session start, replicating OpenClaw's workspace bootstrap. OpenCode only loads
`AGENTS.md` natively; this adds `SOUL.md`, `IDENTITY.md` and `USER.md` (caps:
20k chars per file, 60k total, USER.md 4k). No-op if a file is missing.

Single file (`index.ts`), loaded automatically from the opencode plugins
directory — same pattern as `neoBrain-memory`. Purely local file I/O; no daemon
dependency.

## Install

```bash
ln -s /home/sparo/neobrain/adapters/opencode/persona-bootstrap ~/.opencode/plugins/persona-bootstrap
```

Typecheck: `cd adapters/opencode/persona-bootstrap && npx tsc --noEmit` (config in `tsconfig.json`).

## Environment

| Variable | Default | Meaning |
|---|---|---|
| `OPENCODE_WORKSPACE` | `/home/sparo` | Workspace root holding the persona files. Note: the hardcoded default is sparo-specific; public packaging should make this explicit or derive it from the platform. |
