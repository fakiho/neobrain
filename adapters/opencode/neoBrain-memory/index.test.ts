// Adapter tests: run with `node --test index.test.ts` (Node >= 22.6 with
// --experimental-strip-types, or Node >= 23.6 natively). No dependencies.
// They drive the real plugin through a mock OpenCode context and a mocked
// fetch, so they need neither OpenCode nor a running daemon.
import assert from "node:assert/strict"
import { afterEach, describe, it } from "node:test"
import plugin, { renderPack } from "./index.ts"

type Tool = {
  name: string
  description: string
  input: { properties: Record<string, { description?: string }> }
  execute: (input: Record<string, unknown>, ctx?: { sessionID?: string }) => Promise<{ content: string }>
}

async function loadTools(): Promise<Record<string, Tool>> {
  const tools: Record<string, Tool> = {}
  const ctx = {
    session: { hook: async () => {} },
    tool: {
      transform: async (fn: (e: { namespace: () => void; add: (t: Tool) => void }) => void) =>
        fn({ namespace: () => {}, add: (t) => void (tools[t.name] = t) }),
    },
  }
  await (plugin as { setup: (c: unknown) => Promise<unknown> }).setup(ctx)
  return tools
}

const realFetch = globalThis.fetch
afterEach(() => {
  globalThis.fetch = realFetch
})

function mockFetch(status: number, body: unknown, calls: { url: string; body?: any }[] = []) {
  globalThis.fetch = (async (url: string, init?: { body?: string }) => {
    calls.push({ url: String(url), body: init?.body ? JSON.parse(init.body) : undefined })
    return new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } })
  }) as typeof fetch
  return calls
}

describe("memory_store tool", () => {
  it("tells the agent when to store and when not to", async () => {
    const { store } = await loadTools()
    const d = store.description.toLowerCase()
    assert.match(d, /durable/)
    assert.match(d, /do not call this for chatter/)
    assert.match(d, /do not retry/)
    assert.match(store.input.properties.text.description ?? "", /not a status update/i)
  })

  it("keeps the wake-up hint free of host-specific CLI instructions", () => {
    const text = renderPack({ identity: [{ text: "rule" }] }) ?? ""
    assert.match(text, /memory_store/)
    assert.match(text, /never for chatter/)
    assert.doesNotMatch(text, /neobrain remember/)
  })

  it("posts the text with the live session id and reports success", async () => {
    const { store } = await loadTools()
    const calls = mockFetch(200, { id: "m_abc", type: "preference", hubs: ["agent"], label: "Use tabs" })
    const out = await store.execute({ text: "  Use tabs  ", type: "preference" }, { sessionID: "ses_1" })
    assert.equal(calls[0].body.text, "Use tabs")
    assert.equal(calls[0].body.session, "ses_1")
    assert.match(out.content, /stored \[m_abc\] preference/)
  })

  it("reports a filler rejection as not stored, with the server's code and reason", async () => {
    const { store } = await loadTools()
    mockFetch(422, {
      detail: { stored: false, rejected: true, code: "filler", reason: "memory is conversation filler, not a durable fact" },
    })
    const out = await store.execute({ text: "thanks" }, { sessionID: "ses_1" })
    assert.match(out.content, /not stored \(filler\)/)
    assert.match(out.content, /conversation filler/)
    assert.doesNotMatch(out.content, /HTTP 422/)
    assert.doesNotMatch(out.content, /stored \[/)
  })

  it("reports an unrelated HTTP failure honestly", async () => {
    const { store } = await loadTools()
    mockFetch(500, {})
    const out = await store.execute({ text: "Deploys go out on Tuesdays" })
    assert.match(out.content, /not stored/)
    assert.match(out.content, /HTTP 500/)
  })

  it("reports a down daemon as an error, not a crash", async () => {
    const { store } = await loadTools()
    globalThis.fetch = (async () => {
      throw new Error("connect ECONNREFUSED")
    }) as typeof fetch
    const out = await store.execute({ text: "Deploys go out on Tuesdays" })
    assert.match(out.content, /ECONNREFUSED/)
  })

  it("rejects missing text locally without calling the daemon", async () => {
    const { store } = await loadTools()
    const calls = mockFetch(200, {})
    const out = await store.execute({ text: "   " })
    assert.match(out.content, /missing text/)
    assert.equal(calls.length, 0)
  })

  it("describes supersedes as a user correction or a replaced decision, never a guess", async () => {
    const { store } = await loadTools()
    const d = (store.input.properties.supersedes?.description ?? "").toLowerCase()
    assert.match(d, /correct/)
    assert.match(d, /replac/)
    assert.match(d, /never guess/)
  })

  it("sends supersedes in the remember body, not as a second call", async () => {
    const { store } = await loadTools()
    const calls = mockFetch(200, { id: "m_new", type: "decision", hubs: ["agent"], label: "Use spaces" })
    const out = await store.execute(
      { text: "Use spaces", supersedes: "m_old" },
      { sessionID: "ses_1" },
    )
    assert.equal(calls.length, 1)
    assert.match(calls[0].url, /\/api\/mind\/remember$/)
    assert.equal(calls[0].body.text, "Use spaces")
    assert.equal(calls[0].body.supersedes, "m_old")
    assert.equal(calls[0].body.session, "ses_1")
    assert.match(out.content, /stored \[m_new\]/)
  })

  it("reports not stored when the server rejects the supersedes target", async () => {
    const { store } = await loadTools()
    const calls = mockFetch(400, { detail: "unknown atom: m_old" })
    const out = await store.execute(
      { text: "Use spaces", supersedes: "m_old" },
      { sessionID: "ses_1" },
    )
    assert.equal(calls.length, 1)
    assert.match(out.content, /not stored/)
    assert.match(out.content, /m_old/)
  })

  it("omits supersedes when the caller did not set it", async () => {
    const { store } = await loadTools()
    const calls = mockFetch(200, { id: "m_abc", type: "preference", hubs: ["agent"], label: "Use tabs" })
    await store.execute({ text: "Use tabs" }, { sessionID: "ses_1" })
    assert.equal(calls.length, 1)
    assert.match(calls[0].url, /\/api\/mind\/remember$/)
    assert.equal(calls[0].body.supersedes, undefined)
  })
})

describe("memory_link tool", () => {
  it("posts source, target and kind to /api/mind/link with the live session id", async () => {
    const { link } = await loadTools()
    const calls = mockFetch(200, {
      source: "m_new",
      target: "m_old",
      type: "supersedes",
      explicit: true,
      created: true,
    })
    const out = await link.execute(
      { source_id: "m_new", target_id: "m_old", kind: "supersedes" },
      { sessionID: "ses_link" },
    )
    assert.equal(calls.length, 1)
    assert.match(calls[0].url, /\/api\/mind\/link$/)
    assert.deepEqual(calls[0].body, {
      source_id: "m_new",
      target_id: "m_old",
      kind: "supersedes",
      session: "ses_link",
    })
    assert.match(out.content, /m_new/)
    assert.match(out.content, /m_old/)
    assert.match(out.content, /supersedes/)
  })
})

type Hook = (event: Record<string, unknown>) => void | Promise<void>

async function loadHooks(): Promise<Record<string, Hook>> {
  const hooks: Record<string, Hook> = {}
  const ctx = { session: { hook: async (name: string, fn: Hook) => void (hooks[name] = fn) } }
  await (plugin as { setup: (c: unknown) => Promise<unknown> }).setup(ctx)
  return hooks
}

const ATOMS = [
  { id: "m_top", type: "decision", hub: "neobrain", label: "Ship adapters weekly", text: "Ship adapters weekly on Tuesday." },
  { id: "m_two", type: "lesson", hub: "neobrain", label: "Adapters need tests", text: "Adapters need tests before shipping." },
]

// Route by URL: recall returns ATOMS, everything else is recorded (and may fail).
function mockDaemon(feedbackFails = false) {
  const calls: { url: string; body?: any }[] = []
  globalThis.fetch = (async (url: string, init?: { body?: string }) => {
    const u = String(url)
    calls.push({ url: u, body: init?.body ? JSON.parse(init.body) : undefined })
    if (u.includes("/api/mind/recall"))
      return new Response(JSON.stringify({ atoms: ATOMS }), { status: 200, headers: { "content-type": "application/json" } })
    if (u.includes("/api/mind/feedback") && feedbackFails) throw new Error("connect ECONNREFUSED")
    return new Response("{}", { status: 200, headers: { "content-type": "application/json" } })
  }) as typeof fetch
  return calls
}

async function runTurn(hooks: Record<string, Hook>, sid: string, prompt: string) {
  const system: { type: "text"; text: string }[] = []
  await hooks.prompt({ sessionID: sid, prompt: { text: prompt } })
  await hooks.context({ sessionID: sid, system })
  await new Promise((r) => setTimeout(r, 0)) // let fire-and-forget posts settle
  return system.map((s) => s.text).join("\n")
}

const feedback = (calls: { url: string; body?: any }[]) => calls.filter((c) => c.url.includes("/api/mind/feedback"))

describe("recall `used` signal", () => {
  it("posts `used` exactly once for the inlined top hit", async () => {
    const hooks = await loadHooks()
    const calls = mockDaemon()
    await runTurn(hooks, "ses_used", "when do we ship adapters?")
    const fb = feedback(calls)
    assert.equal(fb.length, 1)
    assert.deepEqual(fb[0].body, { atom_id: "m_top", signal: "used", source: "plugin", session: "ses_used" })
  })

  it("posts nothing for recalled atoms that were only listed by id", async () => {
    const hooks = await loadHooks()
    const calls = mockDaemon()
    const text = await runTurn(hooks, "ses_listed", "when do we ship adapters?")
    assert.match(text, /\[m_two\]/) // listed in the index...
    assert.ok(!feedback(calls).some((c) => c.body.atom_id === "m_two")) // ...but never reported
  })

  it("does not mark the inlined atom `unused` at the turn boundary", async () => {
    const hooks = await loadHooks()
    const calls = mockDaemon()
    await runTurn(hooks, "ses_next", "when do we ship adapters?")
    await runTurn(hooks, "ses_next", "something unrelated xyz")
    const signals = feedback(calls).map((c) => `${c.body.signal}:${c.body.atom_id}`)
    assert.deepEqual(signals.filter((s) => s.endsWith(":m_top")), ["used:m_top"])
    assert.ok(signals.includes("unused:m_two"))
  })

  it("does not throw or lose the injection when the feedback post fails", async () => {
    const hooks = await loadHooks()
    const calls = mockDaemon(true)
    const text = await runTurn(hooks, "ses_fail", "when do we ship adapters?")
    assert.match(text, /Top hit \[m_top\]/)
    assert.equal(feedback(calls).length, 1)
  })

  it("adds one advisory rating hint and no blocking protocol", async () => {
    const hooks = await loadHooks()
    mockDaemon()
    const text = await runTurn(hooks, "ses_hint", "when do we ship adapters?")
    const hintLines = text.split("\n").filter((l) => l.includes("memory_rate"))
    assert.equal(hintLines.length, 1)
    assert.match(hintLines[0], /memory_rate\(id, "useful"\|"noise"\)/)
    assert.doesNotMatch(text, /before you finish|must rate|required/i)
  })
})
