// persona-bootstrap — inject the agent's persona files into the system prompt at
// session start, replicating OpenClaw's workspace bootstrap.
//
// OpenClaw builds its own system prompt and injects AGENTS.md + SOUL.md +
// IDENTITY.md + USER.md every session (order and caps per its docs:
// 20k chars/file, 60k total, USER.md 4k). OpenCode only loads AGENTS.md, so this
// plugin adds the remaining three. Loaded from .opencode/plugins/ (symlink into
// the neobrain repo, same install pattern as neoBrain-memory). No-op if a file
// is missing.

import { readFile } from "node:fs/promises"

type ContextEvent = { sessionID?: string; system?: Array<{ type: string; text: string }> }
type SetupContext = {
  session: {
    hook: (event: string, handler: (e?: ContextEvent) => Promise<void>) => Promise<unknown>
  }
}

const WORKSPACE = process.env.OPENCODE_WORKSPACE ?? "/home/sparo"
const FILES = [
  { name: "SOUL.md", path: `${WORKSPACE}/SOUL.md`, cap: 20000 },
  { name: "IDENTITY.md", path: `${WORKSPACE}/IDENTITY.md`, cap: 20000 },
  { name: "USER.md", path: `${WORKSPACE}/USER.md`, cap: 4000 },
]
const TOTAL_CAP = 60000

export default {
  id: "persona-bootstrap",
  async setup(ctx: SetupContext) {
    const injected = new Set() // sessionID -> already bootstrapped

    await ctx.session.hook("context", async (event) => {
      const sid = event?.sessionID
      const sys = event?.system
      if (!sid || !Array.isArray(sys) || injected.has(sid)) return
      injected.add(sid)

      const parts = []
      let total = 0
      for (const f of FILES) {
        let text
        try {
          text = (await readFile(f.path, "utf8")).trim()
        } catch {
          continue // missing/unreadable → skip, never break the session
        }
        if (!text) continue
        const room = TOTAL_CAP - total
        if (room <= 0) break
        let body = text.length > f.cap ? text.slice(0, f.cap) + "\n…[truncated]" : text
        if (body.length > room) body = body.slice(0, room) + "\n…[truncated]"
        total += body.length
        parts.push(`## ${f.name}\n${body}`)
      }
      if (!parts.length) return
      const text = "# Identity / persona (workspace bootstrap)\n" + parts.join("\n\n")
      sys.push({ type: "text", text })
    })
  },
}
