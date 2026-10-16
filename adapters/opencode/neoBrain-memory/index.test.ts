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
})
