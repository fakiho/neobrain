# USER.md - User Model

Store stable user preferences and profile facts as directives that can guide future sessions.

Use one directive per entry:

```md
<!-- observed: YYYY-MM-DD | status: active -->

- Prefer concise progress updates during implementation work.
```

- Begin each directive with an imperative such as `Always`, `Never`, or `Prefer`.
- Record the observation date and either `active` or `superseded` on the metadata line.
- Save this file at the workspace root as `USER.md`. It loads every session with a separate 4,000-character budget.

## Directives

<!-- observed: 2026-09-20 | status: active -->

- Always spawn subagents with `visible=true`.

<!-- observed: 2026-09-27 | status: active -->

- Prefer an elegant, dark, low-saturation interface. <!-- pin -->

<!-- observed: 2026-09-30 | status: superseded -->

- Prefer TypeScript over Python for new projects.

<!-- observed: 2026-09-30 | status: active -->

- Always communicate straight to the point. <!-- pin -->

## Related

- [Agent workspace](/concepts/agent-workspace)

<!-- observed: 2026-09-28 | status: active -->

- Always write completed work down. <!-- pin -->
