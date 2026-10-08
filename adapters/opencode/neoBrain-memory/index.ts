// neoBrain memory — injects the agent's real memory into the system prompt and
// registers pull tools (open/search) plus a store tool that records the origin
// session.
//
// Three lanes, modelled on OpenClaw's active-memory: the platform PUSHES memory
// into context deterministically instead of relying on the model to call a tool,
// and exposes the same memory for PULL when the model wants more.
//   1. Bootstrap: identity + open loops + active areas, once per session.
//   2. Per-turn recall: recall the user's message against the neoBrain mind and
//      inject a compact ordered index (best hit expanded); escalated on intent.
//   3. Tools: memory_store(text, …) / memory_open(id) / memory_search(query) /
//      memory_rate(id, verdict).
//
// Rating is in-turn and in-session: the recall block asks the agent to rate,
// via memory_rate, only the atoms it actually used, as it finishes its reply.
// Nothing is gated or blocked, and no cross-session state is kept — a session
// can only ever rate what it surfaced and used itself. (The former blocking
// "unrated protocol" was removed: it forced a session to rate atoms another
// session had surfaced, and drove ratings to a near-constant "useful".)
//
// memory_store writes through the plugin so an atom's origin session is the
// live OpenCode sessionID (POST /api/mind/remember with `session`), closing the
// gap where CLI saves land as session_id="cli".
//
// PORT-NOTE: mechanical port of ~/.opencode/plugins/timeline-memory/index.ts
// (timeline → neoBrain rebrand). The source had no imports and no types —
// opencode injects the plugin API at load time — so minimal ambient types below
// mirror that surface for a strict standalone `tsc --noEmit`; annotations were
// added only where strict mode requires them (zero runtime change). Every
// behavioral deviation carries its own PORT-NOTE.
//
// Loaded automatically from the opencode plugins directory. No-op if the
// service is down.

// PORT-NOTE: ambient declaration — this standalone check has no @types/node;
// only `process.env` is used by the plugin.
import { readFile } from "node:fs/promises"

declare const process: { env: Record<string, string | undefined> }

// --- minimal opencode plugin-API surface (see header PORT-NOTE) ---
type ToolExecuteContext = { signal?: AbortSignal; sessionID?: string }
type ToolResult = { content: string }
type ToolDef = {
  name: string
  description: string
  input: {
    type: "object"
    properties: Record<string, unknown>
    required?: string[]
    additionalProperties: false
  }
  options?: { namespace?: string; codemode?: boolean }
  execute: (input: Record<string, unknown>, context?: ToolExecuteContext) => Promise<ToolResult>
}
type ToolEditor = {
  namespace: (def: { name: string; description: string }) => void
  add: (def: ToolDef) => void
}
type HookEvent = {
  sessionID?: string
  prompt?: { text?: string }
  system?: { type: "text"; text: string }[]
}
type PluginContext = {
  session: { hook: (name: string, fn: (event: HookEvent) => void | Promise<void>) => Promise<void> }
  tool?: { transform: (fn: (editor: ToolEditor) => void) => Promise<void> }
}

// PORT-NOTE: env var renamed TIMELINE_API → NEOBRAIN_API; the old hardcoded
// default was already http://127.0.0.1:9192, so the default value is unchanged.
const API = process.env.NEOBRAIN_API ?? "http://127.0.0.1:9192"

// Curated types auto-inject on ordinary turns (OpenClaw restricts auto-injection
// to its curated tier too); observations/dreams/events only surface on intent.
const CURATED = new Set(["preference", "lesson", "decision"])
const INTENT =
  /\b(remind|remember|recall|last time|previously|earlier|what did (we|i)|why did (we|i)|have we|did we|decided|decisions?|lessons?|learned|memory|memories)\b/i

// generic words that would otherwise make the strong-hit gate pass on anything
const STOP = new Set([
  "about", "after", "before", "being", "could", "doing", "every", "other", "please",
  "short", "since", "their", "there", "these", "thing", "things", "those", "tools",
  "until", "using", "which", "while", "would", "should", "where", "makes", "query",
])

// The per-turn block is an ordered INDEX, not an essay: up to IDX_N one-line
// entries (rank order), then the full text of the #1 hit, capped at IDX_CHARS.
const IDX_N = 10
const IDX_CHARS = 2000
const IDX_LABEL = 100

// Standing directives (pinned preferences): the must-follow rules re-injected on
// EVERY model call, not just at wake-up — so time, drift and compaction cannot
// drop them. Kept tiny on purpose: a weak model ignores a long wall of context.
// The block is stable across turns and pushed before the per-turn recall, so it
// sits inside the cacheable system prefix. DIRECTIVES_EVERY_TURN=0 falls back to
// once-per-session.
const DIRECTIVES_EVERY_TURN = String(process.env.NEOBRAIN_DIRECTIVES_EVERY_TURN ?? "1") !== "0"
const DIRECTIVES_N = Number(process.env.NEOBRAIN_DIRECTIVES_N ?? 8)
const DIRECTIVES_CHARS = Number(process.env.NEOBRAIN_DIRECTIVES_CHARS ?? 1000)
const DIRECTIVE_LINE = 180

// Debug trace (test phase): default ON so every lane shows what it actually
// did. NEOBRAIN_DEBUG=0 silences. Output lands in the OpenCode server log
// (journalctl -u opencode.service), every line prefixed [neobrain-memory].
const DEBUG = String((globalThis as unknown as { process?: { env?: Record<string, string> } }).process?.env?.NEOBRAIN_DEBUG ?? "1") !== "0"
const dbg = (msg: string) => {
  if (DEBUG) console.error(`[neobrain-memory] ${new Date().toISOString().slice(11, 23)} ${msg}`)
}
const sidShort = (sid?: string) => (sid ? sid.slice(0, 16) : "nosid")

function oneLine(s: unknown): string {
  return String(s ?? "").replace(/\s+/g, " ").trim()
}
function tokens(s: unknown): string[] {
  return [...new Set(oneLine(s).toLowerCase().match(/[a-z0-9-]{5,}/g) ?? [])].filter((t) => !STOP.has(t))
}

type WakePack = {
  identity?: { text?: string }[]
  open_loops?: { text?: string; label?: string }[]
  hubs?: { label?: string; atoms?: number }[]
}

function renderPack(pack: WakePack | null) {
  if (!pack) return null
  const lines = []
  const id = (pack.identity ?? []).slice(0, 6).map((p) => `- ${p.text}`)
  const open = (pack.open_loops ?? []).slice(0, 5).map((p) => `- ${p.text ?? p.label}`)
  const hubs = (pack.hubs ?? []).slice(0, 8).map((h) => `- ${h.label} (${h.atoms})`)
  if (id.length) lines.push("Identity / rules:\n" + id.join("\n"))
  if (open.length) lines.push("Open loops:\n" + open.join("\n"))
  if (hubs.length) lines.push("Most active areas:\n" + hubs.join("\n"))
  if (!lines.length) return null
  return [
    "# Memory (neoBrain)",
    // PORT-NOTE: CLI names rebranded (timeline → neobrain) per SPEC §2.
    'Recalled from my durable memory at wake-up. Use `neobrain recall "<query>"` for more,',
    'and `neobrain remember "<sentence>" --type … --hubs …` when I decide or learn something durable.',
    "",
    lines.join("\n\n"),
  ].join("\n")
}

type RecallAtom = { id?: string; type?: string; hub?: string; label?: string; text?: string }

// Push a compact, rank-ordered index instead of up to 3 full atoms: a header,
// one line per hit (`[id] type · hub — label`), the #1 hit's full text (truncated
// to fit the budget) and a footer telling the agent how to pull more. No summaries.
function renderRecall(atoms: RecallAtom[], query: string, escalated: boolean) {
  if (!atoms || !atoms.length) return null
  const head = "# Memory (neoBrain) — relevant to this message"
  const sub = `Recalled for "${oneLine(query).slice(0, 160)}"${escalated ? " (deep recall)" : ""}`

  const entries = atoms.slice(0, IDX_N).map(
    (a) => `[${a.id}] ${oneLine(a.type)} · ${oneLine(a.hub)} — ${oneLine(a.label).slice(0, IDX_LABEL)}`,
  )

  const top = atoms[0]
  const topHead = `Top hit [${top.id}] ${oneLine(top.type)} · ${oneLine(top.hub)} — ${oneLine(top.label).slice(0, IDX_LABEL)}`
  const topText = oneLine(top.text)
  const hint = `Before you finish, rate only the memories you actually used: memory_rate(id, "useful") if one helped, "noise" if it misled you. Leave the rest unrated.`
  const footer = `showing ${entries.length} of ${atoms.length} — open one with memory_open(id), or memory_search(query) for more.`

  // Reserve the fixed lines, give the remaining budget to the #1 hit's text.
  const fixed = [head, sub, ...entries, topHead, hint, footer].join("\n").length
  const room = Math.max(0, IDX_CHARS - fixed - 1)
  const body = topText.length > room ? topText.slice(0, Math.max(0, room - 1)).trimEnd() + "…" : topText

  return [head, sub, ...entries, topHead, body, hint, footer].join("\n")
}

type DirectiveAtom = { id?: string; label?: string; text?: string; type?: string }

// Standing directives: a compact imperative block of the pinned must-follow
// rules. Unlike the recall index this carries no atom ids the model is asked to
// rate — it is a push-only lane (same contract as the wake-up pack).
function renderDirectives(pack: { pinned?: boolean; atoms?: DirectiveAtom[] } | null) {
  const atoms = (pack?.atoms ?? []).filter((a) => oneLine(a.text))
  if (!atoms.length) return null
  const head = "# Standing directives (neoBrain) — follow these on every reply"
  const note = pack?.pinned ? "" : " (none pinned yet — showing top preferences)"
  const lines = atoms.slice(0, DIRECTIVES_N).map((a) => `- ${oneLine(a.text).slice(0, DIRECTIVE_LINE)}`)
  const body = [head + note, ...lines].join("\n")
  return body.length > DIRECTIVES_CHARS ? body.slice(0, DIRECTIVES_CHARS - 1).trimEnd() + "…" : body
}

async function fetchDirectives(): Promise<string | null> {
  try {
    const res = await fetch(`${API}/api/mind/directives?limit=${DIRECTIVES_N}`)
    if (!res.ok) {
      dbg(`directives: HTTP ${res.status}`)
      return null
    }
    const text = renderDirectives(await res.json())
    dbg(text
      ? `directives: ${text.length} chars (~${Math.round(text.length / 4)} tok) ${DIRECTIVES_EVERY_TURN ? "every call" : "session"}`
      : "directives: empty — nothing injected")
    return text
  } catch (err: unknown) {
    dbg(`directives FAILED: ${(err as Error)?.message ?? err} (daemon down?)`)
    return null
  }
}

// Lane injection report, fire-and-forget: the daemon cannot see what the plugin
// pushes into the system prompt (its one blind spot), so every injection is
// POSTed to the debug ring for the Observatory's Injections panel. A debug
// failure must never affect a lane — same contract as reportFeedback.
function reportInjection(lane: string, stage: string, chars: number, sessionId?: string, note?: string) {
  try {
    void fetch(`${API}/api/debug/injections`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ session: sessionId, lane, stage, chars, note }),
    })
      .then((r) => {
        if (!r.ok) dbg(`injection report FAILED ${lane}: HTTP ${r.status}`)
      })
      .catch((e: unknown) => dbg(`injection report FAILED ${lane}: ${(e as Error)?.message ?? e}`))
  } catch {
    // ignore
  }
}

// Quality signal, fire-and-forget: a feedback failure must never affect the
// tool result or the push lanes, so nothing is awaited and errors are swallowed.
function reportFeedback(atomId: string, signal: string, sessionId?: string) {
  dbg(`feedback → ${signal} ${atomId}`)
  try {
    void fetch(`${API}/api/mind/feedback`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ atom_id: atomId, signal, source: "plugin", session: sessionId }),
    })
      .then((r) => {
        if (!r.ok) dbg(`feedback FAILED ${atomId}: HTTP ${r.status}`)
      })
      .catch((e: unknown) => dbg(`feedback FAILED ${atomId}: ${(e as Error)?.message ?? e}`))
  } catch {
    // ignore
  }
}

// Full atom + typed connections as plain text (for memory_open).
function formatAtom(a: RecallAtom & { source?: string; tags?: string[]; weight?: number; links?: { source?: string; target?: string; type?: string }[] }) {
  const lines = [`# [${a.id}] ${a.type} · ${a.hub} — ${oneLine(a.label)}`]
  if (a.source) lines.push(`source: ${a.source}`)
  if (Array.isArray(a.tags) && a.tags.length) lines.push(`tags: ${a.tags.join(", ")}`)
  if (typeof a.weight === "number") lines.push(`weight: ${a.weight}`)
  lines.push("", oneLine(a.text) || "(no text)")
  const links = Array.isArray(a.links) ? a.links : []
  if (links.length) {
    lines.push("", "Connections:")
    for (const l of links) {
      const out = l.source === a.id
      lines.push(`- ${out ? "→" : "←"} ${l.type} ${out ? l.target : l.source}`)
    }
  }
  return lines.join("\n")
}

// Pull tools (open/search/rate) plus a store tool. Best effort: a registration
// failure must never break the push lanes, so the caller wraps this in try/catch.
async function registerTools(ctx: PluginContext) {
  // non-null: the caller guards on ctx.tool?.transform before invoking this
  await ctx.tool!.transform((editor) => {
    editor.namespace({ name: "memory", description: "neoBrain durable memory (read + store)" })

    editor.add({
      name: "store",
      description:
        "Store a durable neoBrain memory (decision, lesson, preference, observation). " +
        "Its origin session is this OpenCode session, so prefer this over the CLI for agent saves.",
      input: {
        type: "object",
        properties: {
          text: { type: "string", description: "One clear sentence to remember." },
          type: {
            type: "string",
            enum: ["decision", "lesson", "observation", "preference", "open", "dream", "identity", "correction"],
            description: "Atom type (default observation).",
          },
          hubs: {
            type: "array",
            items: { type: "string" },
            description: "Hub ids to file it under, e.g. [\"neobrain\", \"agent\"].",
          },
          source: { type: "string", description: "Optional provenance, e.g. a file path or 'session:<id>'." },
          label: { type: "string", description: "Optional short label (defaults to the first words of text)." },
          dedupe: { type: "boolean", description: "Return an existing identical atom instead of storing a duplicate." },
        },
        required: ["text"],
        additionalProperties: false,
      },
      options: { namespace: "memory", codemode: true },
      execute: async (input, context) => {
        const text = String(input?.text ?? "").trim()
        if (!text) return { content: "memory_store: missing text" }
        const hubs = Array.isArray(input?.hubs)
          ? (input.hubs as unknown[]).map((h) => String(h).trim()).filter(Boolean)
          : undefined
        try {
          const res = await fetch(`${API}/api/mind/remember`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              text,
              type: typeof input?.type === "string" ? input.type : undefined,
              hubs,
              source: typeof input?.source === "string" ? input.source : undefined,
              label: typeof input?.label === "string" ? input.label : undefined,
              dedupe: input?.dedupe === true,
              session: context?.sessionID, // origin session = this OpenCode session
            }),
            signal: context?.signal,
          })
          if (!res.ok) return { content: `memory_store: HTTP ${res.status}` }
          const a: any = await res.json()
          const hubStr = Array.isArray(a?.hubs) && a.hubs.length ? ` · ${a.hubs.join(",")}` : ""
          const dup = a?.deduped ? " (existing)" : ""
          return {
            content: `memory_store: stored [${a?.id}] ${oneLine(a?.type)}${hubStr}${dup} — ${oneLine(a?.label) || text.slice(0, 60)}`,
          }
        } catch (err: any) {
          return { content: `memory_store: ${err?.message ?? err}` }
        }
      },
    })

    editor.add({
      name: "open",
      description: "Open a neoBrain memory atom by id; returns its full text and typed connections.",
      input: {
        type: "object",
        properties: {
          id: { type: "string", description: "Atom id from the memory index, e.g. m_ab12cd34ef56" },
        },
        required: ["id"],
        additionalProperties: false,
      },
      options: { namespace: "memory", codemode: true },
      execute: async (input, context) => {
        const id = String(input?.id ?? "").trim()
        if (!id) return { content: "memory_open: missing id" }
        try {
          const res = await fetch(`${API}/api/mind/atom/${encodeURIComponent(id)}`, { signal: context?.signal })
          if (!res.ok) return { content: `memory_open: no atom "${id}" (HTTP ${res.status})` }
          const text = formatAtom(await res.json())
          reportFeedback(id, "used", context?.sessionID) // objective usage signal
          return { content: text }
        } catch (err: any) {
          return { content: `memory_open: ${id} — ${err?.message ?? err}` }
        }
      },
    })

    editor.add({
      name: "rate",
      description:
        "Rate a neoBrain memory as useful or noise so future recall ranking learns from it.",
      input: {
        type: "object",
        properties: {
          id: { type: "string", description: "Atom id from the memory index, e.g. m_ab12cd34ef56" },
          verdict: {
            type: "string",
            enum: ["useful", "noise"],
            description: "useful = it helped; noise = misleading or irrelevant",
          },
        },
        required: ["id", "verdict"],
        additionalProperties: false,
      },
      options: { namespace: "memory", codemode: true },
      execute: async (input, context) => {
        const id = String(input?.id ?? "").trim()
        const verdict = String(input?.verdict ?? "").trim().toLowerCase()
        if (!id || (verdict !== "useful" && verdict !== "noise"))
          return { content: `memory_rate: needs an id and a verdict of "useful" or "noise"` }
        try {
          const res = await fetch(`${API}/api/mind/feedback`, {
            method: "POST",
            headers: { "content-type": "application/json" },
            body: JSON.stringify({
              atom_id: id,
              signal: verdict,
              source: "agent",
              session: context?.sessionID,
            }),
          })
          if (!res.ok) return { content: `memory_rate: HTTP ${res.status}` }
          return { content: `memory_rate: recorded "${verdict}" for [${id}]` }
        } catch (err: any) {
          return { content: `memory_rate: ${err?.message ?? err}` }
        }
      },
    })

    editor.add({
      name: "search",
      description: "Search neoBrain durable memory; returns a ranked list of atom ids with type, hub and label.",
      input: {
        type: "object",
        properties: {
          query: { type: "string", description: "What to look for" },
          limit: { type: "integer", minimum: 1, maximum: 25, description: "Max hits (default 10)" },
        },
        required: ["query"],
        additionalProperties: false,
      },
      options: { namespace: "memory", codemode: true },
      execute: async (input, context) => {
        const q = String(input?.query ?? "").trim()
        if (!q) return { content: "memory_search: missing query" }
        const limit = Math.min(25, Math.max(1, Number(input?.limit) || 10))
        try {
          const res = await fetch(
            `${API}/api/mind/recall?q=${encodeURIComponent(q)}&limit=${limit}`,
            { signal: context?.signal },
          )
          if (!res.ok) return { content: `memory_search: HTTP ${res.status}` }
          const data = await res.json()
          const atoms: any[] = Array.isArray(data?.atoms) ? data.atoms : []
          if (!atoms.length) return { content: `memory_search: no matches for "${q}"` }
          const lines = atoms.map(
            (a, i) => `${i + 1}. [${a.id}] ${oneLine(a.type)} · ${oneLine(a.hub)} — ${oneLine(a.label)}`,
          )
          return { content: `# Memory — "${q}" (${atoms.length} ranked hits)\n` + lines.join("\n") }
        } catch (err: any) {
          return { content: `memory_search: ${err?.message ?? err}` }
        }
      },
    })
  })
}

// Persona docs injected once per session (caps per OpenClaw's bootstrap:
// 20k chars/file, 60k total, USER.md 4k).
// Workspace root for the persona docs; default to $HOME so the plugin is
// portable (no hardcoded personal path).
const WORKSPACE = process.env.OPENCODE_WORKSPACE ?? process.env.HOME ?? "."
type PersonaFile = { name: string; path: string; cap: number; compact?: boolean }
const PERSONA_FILES: PersonaFile[] = [
  { name: "SOUL.md", path: `${WORKSPACE}/SOUL.md`, cap: 20000 },
  { name: "IDENTITY.md", path: `${WORKSPACE}/IDENTITY.md`, cap: 20000 },
  { name: "USER.md", path: `${WORKSPACE}/USER.md`, cap: 4000, compact: true },
]
const PERSONA_TOTAL_CAP = 60000

// USER.md is a directive ledger that only grows: it carries a format explainer,
// dated metadata comments, superseded entries, inline pin markers and a footer —
// none of which the model needs. The raw file already exceeds the 4k cap, so its
// tail (real, active rules) was being silently truncated. Compact to just the
// active directive bullets before the cap applies — deterministic, no model, and
// no loss of an active rule however much boilerplate the file accumulates.
function compactUserDoc(text: string): string {
  const out: string[] = []
  let started = false
  let active = true
  let fence = false
  for (const raw of text.split("\n")) {
    // The format explainer shows a sample entry inside a fenced code block; its
    // metadata line must not be mistaken for the first real directive.
    if (/^\s*```/.test(raw)) {
      fence = !fence
      continue
    }
    if (fence) continue
    const line = raw.replace(/<!--\s*pin\s*-->/gi, "").trimEnd()
    const meta = line.match(/^<!--\s*observed:.*status:\s*(\w+).*-->\s*$/i)
    if (meta) {
      started = true
      active = meta[1].toLowerCase() === "active"
      continue
    }
    if (!started) continue // drop the header/format explainer before the first entry
    const t = line.trim()
    if (!t || t.startsWith("#")) continue
    if (/^-\s*\[[^\]]+\]\([^)]*\)\s*$/.test(t)) continue // link-only bullet (Related footer)
    if (!active) continue // superseded entry
    out.push(t)
  }
  return out.join("\n")
}

export default {
  id: "neobrain-memory", // PORT-NOTE: rebranded plugin id (was "timeline-memory"); loading pattern unchanged.
  async setup(ctx: PluginContext) {
    dbg(`plugin loaded — api=${API}${DEBUG ? "" : " (debug off via NEOBRAIN_DEBUG=0)"}`)
    const woken = new Set<string>()
    const pending = new Map<string, string>() // sessionID -> the user's text for the current turn
    const personaDone = new Set<string>() // sessionID -> persona docs already injected
    const directivesText = new Map<string, string | null>() // sessionID -> rendered standing directives
    const directivesDone = new Set<string>() // sessionID -> directive block already injected (EVERY_TURN=0 only)
    const postCompact = new Set<string>() // sessionID -> compaction ran, next context call re-injects

    // Push the standing-directives block, logging the stage it landed in:
    //   @context    — a normal agent-loop call (the reply itself)
    //   @compaction — the summary request, so the rules survive the compact
    // The once-per-session guard only applies to normal calls: the compaction
    // request is a separate model call that must always carry the rules.
    const pushDirectives = async (
      sid: string,
      sys: NonNullable<HookEvent["system"]>,
      stage: "context" | "compaction",
      reason = "",
    ) => {
      if (!DIRECTIVES_EVERY_TURN && stage !== "compaction" && directivesDone.has(sid)) return
      directivesDone.add(sid)
      let d = directivesText.get(sid)
      if (d === undefined) {
        d = await fetchDirectives()
        directivesText.set(sid, d)
      }
      if (!d) return
      sys.push({ type: "text", text: d })
      dbg(`${sidShort(sid)} directives: injected @${stage} (${d.length} chars)${reason}${DIRECTIVES_EVERY_TURN ? "" : " (once)"}`)
      reportInjection("directives", stage, d.length, sid, stage === "compaction" ? "summary request" : reason ? "post-compaction" : undefined)
    }

    // Capture the user's message at admission; the context hook injects it.
    await ctx.session.hook("prompt", (event) => {
      const sid = event?.sessionID
      const text = oneLine(event?.prompt?.text)
      if (sid && text) pending.set(sid, text)
    })

    await ctx.session.hook("context", async (event) => {
      const sid = event?.sessionID
      const sys = event?.system
      if (!sid || !Array.isArray(sys)) return
      // Read-and-clear: only the first call after a compaction reports the reason.
      const reason = postCompact.delete(sid) ? " (post-compaction)" : ""

      // 1) bootstrap: identity + open loops + active areas, once per session.
      // (PORT-NOTE: left ungated on purpose — the wakeup pack carries no atom
      // ids, so there is nothing to register pending, and it is not a
      // model-initiated mind call.)
      if (!woken.has(sid)) {
        woken.add(sid)
        dbg(`${sidShort(sid)} wakeup: fetching bootstrap pack`)
        try {
          const res = await fetch(`${API}/api/mind/wakeup?session=${encodeURIComponent(sid)}`)
          if (res.ok) {
            const text = renderPack(await res.json())
            if (text) {
              sys.push({ type: "text", text })
              dbg(`${sidShort(sid)} wakeup: pack injected (${text.length} chars)${reason}`)
              reportInjection("wakeup", "context", text.length, sid, reason ? "post-compaction" : undefined)
            } else dbg(`${sidShort(sid)} wakeup: pack empty — nothing injected`)
          } else dbg(`${sidShort(sid)} wakeup: HTTP ${res.status}`)
        } catch (err: unknown) {
          dbg(`${sidShort(sid)} wakeup FAILED: ${(err as Error)?.message ?? err} (daemon down? no injection this session)`)
        }
      }

      // persona lane (merged from the former standalone persona-bootstrap
      // plugin, 2026-10-01): inject the workspace persona docs once per
      // session — OpenClaw parity for the files OpenCode doesn't load
      // natively (AGENTS.md only). Local file I/O, deliberately independent
      // of the daemon so it survives downtime.
      if (!personaDone.has(sid)) {
        personaDone.add(sid)
        const parts: string[] = []
        let total = 0
        for (const f of PERSONA_FILES) {
          let text
          try {
            text = (await readFile(f.path, "utf8")).trim()
          } catch {
            continue // missing/unreadable → skip, never break the session
          }
          if (!text) continue
          if (f.compact) text = compactUserDoc(text)
          if (!text) continue
          const room = PERSONA_TOTAL_CAP - total
          if (room <= 0) break
          let body = text.length > f.cap ? text.slice(0, f.cap) + "\n…[truncated]" : text
          if (body.length > room) body = body.slice(0, room) + "\n…[truncated]"
          total += body.length
          parts.push(`## ${f.name}\n${body}`)
        }
        if (parts.length) {
          const text = "# Identity / persona (workspace bootstrap)\n" + parts.join("\n\n")
          sys.push({ type: "text", text })
          dbg(`${sidShort(sid)} persona: injected (${text.length} chars)${reason}`)
          reportInjection("persona", "context", text.length, sid, reason ? "post-compaction" : undefined)
        }
      }

      // standing-directives lane: the pinned must-follow rules, re-injected on
      // every model call (fetched once per session, then pushed from cache).
      // Sit it before the per-turn recall so the block stays in the cacheable
      // system prefix; it is push-only and carries no rateable atom ids.
      await pushDirectives(sid, sys, "context", reason)

      // 2) per-turn deterministic recall (lane 1), escalated by intent (lane 2)
      const query = pending.get(sid)
      if (!query) return
      pending.delete(sid)

      const escalated = INTENT.test(query)
      const want = tokens(query)
      try {
        const url =
          `${API}/api/mind/recall?q=${encodeURIComponent(query.slice(0, 300))}` +
          `&limit=${escalated ? 25 : 15}&session=${encodeURIComponent(sid)}`
        const res = await fetch(url)
        if (!res.ok) {
          dbg(`${sidShort(sid)} recall: HTTP ${res.status}`)
          return
        }
        const data = await res.json()
        let atoms: any[] = Array.isArray(data?.atoms) ? data.atoms : []
        if (!escalated) atoms = atoms.filter((a) => CURATED.has(a.type))
        // strong-hit gate: require a meaningful (>=5 char) query token in the atom
        atoms = atoms.filter((a) => {
          const hay = oneLine(`${a.label} ${a.text}`).toLowerCase()
          return want.some((t) => hay.includes(t))
        })
        const text = renderRecall(atoms, query, escalated)
        if (text) {
          sys.push({ type: "text", text })
          dbg(`${sidShort(sid)} recall "${oneLine(query).slice(0, 60)}" escalated=${escalated} → ${Math.min(atoms.length, IDX_N)} hits injected`)
          reportInjection("recall", "context", text.length, sid, escalated ? "deep" : undefined)
        } else {
          dbg(`${sidShort(sid)} recall "${oneLine(query).slice(0, 60)}" escalated=${escalated} → 0 hits after gates, nothing injected`)
        }
      } catch (err: unknown) {
        dbg(`${sidShort(sid)} recall FAILED: ${(err as Error)?.message ?? err} (daemon down?)`)
      }
    })

    // Compaction rebuilds the transcript, but the persona and wake-up lanes are
    // gated once-per-session — without this they would never return after a
    // summary. Keep the standing rules visible to the summariser, and reset the
    // once-per-session gates so the next normal call re-injects persona+wakeup.
    await ctx.session.hook("compaction", async (event) => {
      const sid = event?.sessionID
      const sys = event?.system
      if (!sid || !Array.isArray(sys)) return
      await pushDirectives(sid, sys, "compaction")
      woken.delete(sid)
      personaDone.delete(sid)
      directivesDone.delete(sid)
      directivesText.delete(sid)
      pending.delete(sid)
      postCompact.add(sid)
      dbg(`${sidShort(sid)} compaction: directives @compaction; persona+wakeup re-inject next turn`)
    })

    // 3) memory tools (memory_store / memory_open / memory_search / memory_rate)
    try {
      if (ctx.tool?.transform) await registerTools(ctx)
    } catch (err: any) {
      console.error("[neobrain-memory] tool registration failed:", err?.message ?? err)
    }
  },
}
