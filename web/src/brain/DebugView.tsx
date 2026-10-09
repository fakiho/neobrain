import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Bug } from 'lucide-react'

// Debug page (SPEC §5 visibility): one clean view of what the brain and the
// plugin actually did — no journal diving required. Everything here comes from
// the daemon's own records: /api/debug/overview (DB aggregates + life-loop
// state), /api/debug/requests and /api/debug/injections (rolling traces).
//
// Layout: a status-first ribbon (derived health) on top, an attention queue, and
// a tiled 3x3 board of nine equal cards that fits one screen. Each card scrolls
// internally; the raw streams peek at the last few calls until expanded. The
// board's height is pinned to the viewport budget in JS, so opening the config
// card below it grows the page instead of squeezing the cards above.

type Req = {
  ts: number
  method: string
  path: string
  query: string
  status: number
  ms: number
  session: string
}

type Inj = {
  ts: number
  session: string | null
  lane: string
  stage: string
  chars: number
  note: string | null
}

type Overview = {
  now: number
  daemon: {
    pid: number
    started_at: number | null
    uptime_s: number | null
    supervised: boolean
    manager: string
    service: string
    bind: string
    api: string
    life_enabled: boolean
  }
  counts: Record<string, number>
  rank_states: { state: string; n: number }[]
  atom_types: { type: string; n: number }[]
  ops_today: { op: string; n: number }[]
  recent_ops: { ts: number; op: string; atom_id: string | null; label: string | null; query: string | null; session_id: string | null }[]
  feedback: { ts: number; atom_id: string; signal: string; source: string; session_id: string | null; origin_session_id: string | null }[]
  ingest: { started_at: number; finished_at: number | null; source: string; added: number; updated: number; errors: number; note: string | null }[]
  phases: { phase: string; every_minutes: number; last: { ts: number; summary: string; next_due: number } | null }[]
  quiet_now: boolean
  last_dream: { ts: number; label: string } | null
  rating_watch: {
    enabled: boolean
    days: number
    hour: number
    until: string
    current: {
      days: number
      since_ms: number
      at_ms: number
      useful: number
      noise: number
      used: number
      raters: number
      rated: number
      unranked: number
      atoms: number
    }
    last: { ts: number; severity: string; summary: string; next_due: number | null; snapshot: Record<string, number> } | null
  }
  self_trace: {
    docs: { doc_path: string; ts: number; content_hash: string }[]
    doc_counts: { doc_path: string; versions: number; last_ts: number }[]
    preferences: { id: string; label: string; created: number }[]
  }
  config: Record<string, string | number | boolean>
}

const PEEK = 3

const ago = (ts?: number | null) => {
  if (!ts) return '—'
  const s = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (s < 5) return 'just now'
  if (s < 60) return `${s}s ago`
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}
const inMins = (ts?: number | null) => {
  if (!ts) return '—'
  const m = Math.round((ts - Date.now()) / 60000)
  return m <= 0 ? 'due now' : `in ${m}m`
}
const shortSid = (sid?: string | null) => (sid ? sid.replace(/^ses_/, '').slice(0, 10) : '')
const clock = (ts?: number | null) => (ts ? new Date(ts).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' }) : '—')
const trunc = (s?: string | null, n = 60) => {
  const t = String(s ?? '')
  return t.length > n ? `${t.slice(0, n - 1)}…` : t
}
const fmtUptime = (s?: number | null) => {
  if (s == null) return '—'
  const d = Math.floor(s / 86400)
  const h = Math.floor((s % 86400) / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (d) return `${d}d ${h}h`
  if (h) return `${h}h ${m}m`
  return `${m}m`
}
// Severity of a journal line, for coloring: INFO / WARNING / ERROR (uvicorn and
// Python logging both emit the level word before the message).
const logLevel = (line: string) => {
  const m = line.match(/\b(DEBUG|TRACE|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b/)
  switch (m?.[1]) {
    case 'ERROR':
    case 'CRITICAL':
    case 'FATAL':
      return 'dbg-lvl-error'
    case 'WARNING':
    case 'WARN':
      return 'dbg-lvl-warn'
    case 'INFO':
      return 'dbg-lvl-info'
    case undefined:
      return ''
    default:
      return 'dbg-lvl-debug'
  }
}

const OP_COLOR: Record<string, string> = {
  store: '#7fd18a',
  recall: '#8fb3ff',
  feedback: '#e8c66b',
  dream: '#c39be0',
  link: '#6ecfc4',
  forget: '#e08585',
}

// The plugin's push lanes — what it injected into the model's system prompt.
// The daemon never witnesses these directly; the plugin reports them.
const INJ_COLOR: Record<string, string> = {
  wakeup: '#8fb3ff',
  persona: '#c39be0',
  directives: '#7fd18a',
  recall: '#e8c66b',
  notice: '#e08585',
}

const statusColor = (s: number) => (s < 300 ? 'var(--ok)' : s < 400 ? 'var(--text-muted)' : 'var(--err)')
const latColor = (ms: number) => (ms < 50 ? 'var(--ok)' : ms < 200 ? 'var(--warn)' : 'var(--err)')

// Which producer made this call — the OpenCode plugin (wakeup/recall/feedback),
// the dashboard itself (graph/activity polling), or something else (CLI, tests).
const laneTag = (r: Req) =>
  r.path.startsWith('/api/mind/wakeup') || r.path.startsWith('/api/mind/recall') || r.path === '/api/mind/feedback'
    ? 'plugin'
    : r.path.startsWith('/api/mind/activity') || r.path.startsWith('/api/mind/graph')
      ? 'dashboard'
      : 'other'

type CallGroup = {
  key: string
  ts: number
  tsMin: number
  n: number
  ms: number
  msMax: number
  status: number
  method: string
  path: string
  query: string
  session: string
  sessions: Set<string>
  lane: string
}

// Collapse consecutive identical calls (same method+path+status within 45s) so a
// burst of polling shows as one row with a ×N count instead of drowning the pane.
function groupCalls(rows: Req[]): CallGroup[] {
  const out: CallGroup[] = []
  for (const r of rows) {
    const last = out[out.length - 1]
    const key = `${r.method} ${r.path} ${r.status}`
    if (last && last.key === key && last.tsMin - r.ts < 45000) {
      last.n++
      last.tsMin = r.ts
      last.msMax = Math.max(last.msMax, r.ms)
      last.sessions.add(r.session || '')
    } else {
      out.push({
        key, ts: r.ts, tsMin: r.ts, n: 1, ms: r.ms, msMax: r.ms, status: r.status,
        method: r.method, path: r.path, query: r.query, session: r.session,
        sessions: new Set([r.session || '']), lane: laneTag(r),
      })
    }
  }
  return out
}

type Level = 'ok' | 'warn' | 'err' | 'info' | 'unknown'
type HealthItem = { level: 'warn' | 'err' | 'info'; title: string; detail: string }
type RibbonKey = 'daemon' | 'life' | 'rating' | 'ingest' | 'api' | 'lanes'

const RIBBON_ORDER: RibbonKey[] = ['daemon', 'life', 'rating', 'ingest', 'api', 'lanes']
const RIBBON_LABEL: Record<RibbonKey, string> = {
  daemon: 'daemon', life: 'life loop', rating: 'rating watch', ingest: 'ingest', api: 'api', lanes: 'plugin lanes',
}

const rate = (reqs: Req[]) => {
  const now = Date.now()
  return reqs.filter((r) => now - r.ts <= 300000).length / 5
}

// Derive the health ribbon + an attention queue from the raw data. This is the
// whole point of the view: turn a pile of records into "what needs looking at".
function health(ov: Overview, reqs: Req[], injs: Inj[]) {
  const items: HealthItem[] = []
  const now = Date.now()
  const h = {} as Record<RibbonKey, { level: Level; value: string }>

  const up = ov.daemon.uptime_s
  const dlevel: Level = ov.daemon.supervised ? 'ok' : 'err'
  h.daemon = { level: dlevel, value: fmtUptime(up) }
  if (!ov.daemon.supervised) items.push({ level: 'err', title: 'Daemon is not supervised', detail: 'running as an orphan — no restart if it crashes' })
  else if (up != null && up < 120) items.push({ level: 'warn', title: 'Daemon restarted recently', detail: `up ${fmtUptime(up)} — request/injection buffers refill as traffic resumes` })

  let llevel: Level = 'ok'
  const overdue: string[] = []
  for (const p of ov.phases) {
    const nd = p.last?.next_due
    if (p.last && nd && now > nd + Math.max(120000, (p.every_minutes || 10) * 60000 * 0.5)) overdue.push(p.phase)
  }
  if (overdue.length) {
    llevel = 'warn'
    items.push({ level: 'warn', title: 'Life phase overdue', detail: `${overdue.join(', ')} past due — the loop may be stuck` })
  }
  h.life = { level: llevel, value: ov.quiet_now ? 'quiet' : 'awake' }
  if (ov.quiet_now) items.push({ level: 'info', title: 'Quiet hours', detail: 'only perceive runs; reflect/act/dream are paused until morning' })

  let rlevel: Level = 'unknown'
  let rval = 'off'
  if (ov.rating_watch.enabled) {
    rlevel = 'ok'
    rval = `${ov.rating_watch.current.useful}u / ${ov.rating_watch.current.noise}n`
    if (ov.rating_watch.last?.severity === 'warning') {
      rlevel = 'warn'
      items.push({ level: 'warn', title: 'Rating watch stalled', detail: 'a snapshot found zero ratings in the window — memory feedback has gone quiet' })
    }
  }
  h.rating = { level: rlevel, value: rval }

  const ing = ov.ingest
  const lastRun = ing[0]
  let ilevel: Level = 'ok'
  const ival = lastRun ? ago(lastRun.started_at) : 'none'
  if (ing.slice(0, 5).some((r) => r.errors > 0)) {
    ilevel = 'warn'
    const r = ing.find((x) => x.errors > 0)!
    items.push({ level: 'warn', title: 'Ingest errors', detail: `${r.source} reported ${r.errors} error(s) ${ago(r.started_at)}` })
  }
  if (lastRun && !lastRun.finished_at && now - lastRun.started_at > 15 * 60000) {
    ilevel = 'warn'
    items.push({ level: 'warn', title: 'Ingest run appears stuck', detail: `${lastRun.source} started ${ago(lastRun.started_at)} and never finished` })
  }
  h.ingest = { level: ilevel, value: ival }

  const errs = reqs.filter((r) => r.status >= 400)
  const e5 = errs.filter((r) => r.status >= 500)
  const lats = reqs.map((r) => r.ms).sort((a, b) => a - b)
  const p95 = lats.length ? lats[Math.floor(lats.length * 0.95)] ?? lats[lats.length - 1] : 0
  const alevel: Level = e5.length ? 'err' : errs.length ? 'warn' : p95 > 1000 ? 'warn' : 'ok'
  h.api = { level: alevel, value: `${errs.length ? `${errs.length} err` : 'ok'} · p95 ${Math.round(p95)}ms` }
  if (e5.length) items.push({ level: 'err', title: 'Server errors (5xx)', detail: `${e5.length} of the last ${reqs.length} calls returned 5xx` })
  else if (errs.length) items.push({ level: 'warn', title: 'Client errors (4xx)', detail: `${errs.length} of the last ${reqs.length} calls returned 4xx` })
  if (!errs.length && p95 > 1000) items.push({ level: 'warn', title: 'Slow API responses', detail: `p95 latency ${Math.round(p95)}ms over the last ${reqs.length} calls` })

  const nlevel: Level = injs.length ? 'ok' : 'unknown'
  h.lanes = { level: nlevel, value: injs.length ? `${injs.length} pushes` : 'none yet' }
  if (!injs.length) items.push({ level: 'info', title: 'No lane injections buffered', detail: 'needs a new OpenCode session, or the daemon restarted and the buffer is empty' })

  const order = { err: 0, warn: 1, info: 2 }
  items.sort((a, b) => order[a.level] - order[b.level])

  return { ribbon: h, items, p95, errs: errs.length, e5: e5.length, callsPerMin: rate(reqs) }
}

// A 30-bar sparkline of calls per 10s over the last 5 minutes.
function sparkBars(reqs: Req[]) {
  const now = Date.now()
  const B = 30
  const W = 5000
  const c = new Array(B).fill(0)
  for (const r of reqs) {
    const d = now - r.ts
    if (d < 0 || d >= B * W) continue
    c[B - 1 - Math.floor(d / W)]++
  }
  const max = Math.max(1, ...c)
  return c.map((v) => ({
    h: Math.max(2, Math.round((v / max) * 20)),
    o: v ? (0.3 + 0.7 * (v / max)).toFixed(2) : '0.18',
    hot: v === max && max > 1,
  }))
}

// Pin the board to exactly fill the space left after the header/ribbon/attention,
// so the nine cards are equal and fit one screen. The board's height is fixed to
// the viewport budget (independent of the config card below it), so expanding
// config adds page height and scrolls — it never squeezes the cards above.
function fitBoard() {
  const scroller = document.querySelector('.debug') as HTMLElement | null
  const wrap = document.querySelector('.dbg-wrap') as HTMLElement | null
  const board = document.querySelector('.dbg-board') as HTMLElement | null
  if (!scroller || !wrap || !board) return
  if (getComputedStyle(board).display === 'flex') { board.style.height = ''; return } // narrow: normal flow
  const cfg = document.getElementById('dbg-card-config')
  const gap = 11
  const cs = getComputedStyle(scroller)
  const avail0 = scroller.clientHeight - parseFloat(cs.paddingTop || '0') - parseFloat(cs.paddingBottom || '0')
  let chrome = 0
  let visible = 0
  for (const el of Array.from(wrap.children) as HTMLElement[]) {
    if (el === board) continue
    if (el === cfg) { chrome += 40; visible++; continue } // collapsed config strip only
    if (el.offsetParent === null) continue // hidden / fixed drawers
    chrome += el.getBoundingClientRect().height
    visible++
  }
  visible++ // the board itself
  const avail = avail0 - chrome - (visible - 1) * gap
  board.style.height = `${Math.max(300, Math.round(avail))}px`
}

export function DebugView() {
  const [ov, setOv] = useState<Overview | null>(null)
  const [reqs, setReqs] = useState<Req[]>([])
  const [injs, setInjs] = useState<Inj[]>([])
  const [err, setErr] = useState(false)
  const [paused, setPaused] = useState(false)

  // Only Effective config collapses; the nine board cards are always open.
  const [configOpen, setConfigOpen] = useState(false)
  const [filter, setFilter] = useState<'all' | 'plugin' | 'dashboard' | 'other'>('all')
  const [errorsOnly, setErrorsOnly] = useState(false)
  const [callsFull, setCallsFull] = useState(false)
  const [injFull, setInjFull] = useState(false)
  const [flashReq, setFlashReq] = useState<Set<number>>(new Set())
  const [flashInj, setFlashInj] = useState<Set<number>>(new Set())

  // Daemon journal is pulled on demand only — never in the 5s poll.
  const [logs, setLogs] = useState<string[] | null>(null)
  const [logsOpen, setLogsOpen] = useState(false)
  const [logsErr, setLogsErr] = useState<string | null>(null)

  const prevReqMax = useRef(0)
  const prevInjMax = useRef(0)

  const load = useCallback(() => {
    Promise.all([
      fetch('/api/debug/overview').then((r) => (r.ok ? r.json() : Promise.reject(new Error('x')))),
      fetch('/api/debug/requests?limit=200').then((r) => (r.ok ? r.json() : Promise.reject(new Error('x')))),
      fetch('/api/debug/injections?limit=200').then((r) => (r.ok ? r.json() : Promise.reject(new Error('x')))),
    ])
      .then(([o, rq, ij]) => {
        setOv(o as Overview)
        setErr(false)
        const rs: Req[] = rq.requests ?? []
        const is: Inj[] = ij.injections ?? []
        setReqs(rs)
        setInjs(is)
        const rMax = rs.reduce((m, r) => Math.max(m, r.ts), 0)
        const iMax = is.reduce((m, r) => Math.max(m, r.ts), 0)
        if (prevReqMax.current) {
          const s = new Set<number>()
          for (const r of rs) if (r.ts > prevReqMax.current) s.add(r.ts)
          if (s.size) setFlashReq(s)
        }
        if (prevInjMax.current) {
          const s = new Set<number>()
          for (const r of is) if (r.ts > prevInjMax.current) s.add(r.ts)
          if (s.size) setFlashInj(s)
        }
        prevReqMax.current = rMax
        prevInjMax.current = iMax
      })
      .catch(() => setErr(true))
  }, [])

  const loadLogs = useCallback(() => {
    setLogs(null)
    setLogsErr(null)
    fetch('/api/debug/logs?lines=300')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
      .then((d) => {
        setLogs(d.lines ?? [])
        setLogsErr(d.error ?? null)
      })
      .catch(() => setLogsErr('fetch failed'))
  }, [])

  useEffect(() => {
    if (!paused) load()
    const t = window.setInterval(() => { if (!paused) load() }, 5000)
    return () => window.clearInterval(t)
  }, [paused, load])

  // Clear the "new" flash a beat after it appears.
  useEffect(() => {
    if (flashReq.size === 0 && flashInj.size === 0) return
    const t = window.setTimeout(() => {
      setFlashReq(new Set())
      setFlashInj(new Set())
    }, 2400)
    return () => window.clearTimeout(t)
  }, [flashReq, flashInj])

  // Keep the board fitted after every render and on any resize.
  useLayoutEffect(() => { fitBoard() })
  useEffect(() => {
    const onR = () => fitBoard()
    window.addEventListener('resize', onR)
    const ro = new ResizeObserver(onR)
    const el = document.querySelector('.debug')
    if (el) ro.observe(el)
    document.fonts?.ready?.then(onR).catch(() => {})
    return () => {
      window.removeEventListener('resize', onR)
      ro.disconnect()
    }
  }, [])

  const h = useMemo(() => (ov ? health(ov, reqs, injs) : null), [ov, reqs, injs])
  const grouped = useMemo(() => {
    let rows = [...reqs].reverse()
    if (filter !== 'all') rows = rows.filter((r) => laneTag(r) === filter)
    if (errorsOnly) rows = rows.filter((r) => r.status >= 400)
    return groupCalls(rows)
  }, [reqs, filter, errorsOnly])
  const callsShown = callsFull ? grouped : grouped.slice(0, PEEK)
  const injRows = useMemo(() => [...injs].reverse(), [injs])
  const injShown = injFull ? injRows : injRows.slice(0, PEEK)
  const spark = useMemo(() => sparkBars(reqs), [reqs])

  const scrollTo = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })

  const stat = (label: string, value: string | number) => (
    <div className="dbg-stat" key={label}>
      <div className="v">{value}</div>
      <div className="l">{label}</div>
    </div>
  )

  return (
    <div className="debug">
      <div className="dbg-wrap">
        <header className="dbg-head">
          <div>
            <h1><Bug size={18} /> Debug</h1>
            <p className="sub">Status first · raw streams collapse to the last few calls until you expand them.</p>
          </div>
          <div className="dbg-head-r">
            <span className="dbg-btn on" style={{ cursor: 'default' }}>
              <span className="dbg-dot" style={{ background: err ? 'var(--err)' : paused ? 'var(--warn)' : 'var(--ok)' }} />
              {err ? 'daemon unreachable' : paused ? 'paused' : 'live · 5s'}
            </span>
            <button className="dbg-btn" onClick={() => setPaused((p) => !p)}>{paused ? 'Resume' : 'Pause'}</button>
            <button className="dbg-btn" onClick={() => { if (logsOpen) setLogsOpen(false); else { setLogsOpen(true); loadLogs() } }}>
              {logsOpen ? 'hide logs' : 'daemon logs'}
            </button>
          </div>
        </header>

        {err && <div className="dbg-banner">Daemon unreachable — showing the last snapshot.</div>}

        {h && ov && (
          <>
            <div className="dbg-sticky">
              <div className="dbg-ribbon">
                {RIBBON_ORDER.map((k) => {
                  const x = h.ribbon[k]
                  const target = k === 'api' ? 'dbg-card-calls' : k === 'lanes' ? 'dbg-card-inj' : `dbg-card-${k}`
                  return (
                    <div
                      key={k}
                      className={`dbg-health ${x.level}`}
                      role="button"
                      tabIndex={0}
                      title={`jump to ${k}`}
                      onClick={() => scrollTo(target)}
                      onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); scrollTo(target) } }}
                    >
                      <span className="dbg-dot" />
                      <span className="k">{RIBBON_LABEL[k]}</span>
                      <span className="v">{x.value}</span>
                    </div>
                  )
                })}
              </div>
              <div className="dbg-summary">
                <span><b>{ov.counts.atoms}</b> atoms</span>
                <span><b>{ov.counts.hubs}</b> hubs</span>
                <span><b>{ov.counts.edges}</b> links</span>
                <span><b>{ov.counts.ops_today}</b> ops today</span>
                <span className="sep">|</span>
                <span>{h.callsPerMin.toFixed(1)} calls/min</span>
                <span>p95 <b>{Math.round(h.p95)}ms</b></span>
                <span style={{ color: h.errs ? 'var(--err)' : undefined }}><b>{h.errs}</b> errors</span>
                <span className="sep">|</span>
                <span>feedback 24h <b>{ov.counts.feedback_24h}</b></span>
                <span>unranked <b>{ov.counts.unranked}</b></span>
              </div>
            </div>

            <div className="dbg-attn">
              {h.items.length === 0 ? (
                <div className="dbg-attn-clear"><span className="dbg-dot" /> All systems nominal — nothing needs attention.</div>
              ) : (
                <div className="dbg-attn-list" tabIndex={0}>
                  {h.items.map((i, idx) => (
                    <div key={idx} className={`dbg-attn-item ${i.level}`}>
                      <div><div className="t">{i.title}</div><div className="d">{i.detail}</div></div>
                    </div>
                  ))}
                </div>
              )}
            </div>
          </>
        )}

        {logsOpen && (
          <div className="dbg-card dbg-drawer">
            <h5>
              Daemon logs — journal tail
              <span className="dbg-tag">{logs?.length ?? 0} lines</span>
              <button className="dbg-btn" style={{ marginLeft: 'auto' }} onClick={loadLogs}>refresh</button>
              <button className="dbg-btn" style={{ marginLeft: 6 }} onClick={() => setLogsOpen(false)}>close</button>
            </h5>
            <div className="dbg-body">
              {logsErr && <p className="muted" style={{ margin: '0 0 6px' }}>journal unavailable: {logsErr}</p>}
              <div className="dbg-logs" tabIndex={0}>
                {logs
                  ? (logs.length ? logs.map((l, i) => <div key={i} className={logLevel(l)}>{l}</div>) : <div>(no lines)</div>)
                  : <div>loading…</div>}
              </div>
            </div>
          </div>
        )}

        {!ov && <div className="muted" style={{ padding: 20 }}>loading…</div>}

        {ov && h && (
          <>
            <div className="dbg-board">
              {/* ---- live calls ---- */}
              <section className="dbg-card" id="dbg-card-calls">
                <h5>
                  <span className="dbg-dot" style={{ background: 'var(--info)' }} /> Live calls
                  <span className="dbg-tag">{reqs.length} buffered</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-sub-head">
                    <div className="dbg-spark" title="calls per 10s, last 5 min">
                      {spark.map((b, i) => <i key={i} className={b.hot ? 'hot' : ''} style={{ height: b.h, opacity: b.o }} />)}
                    </div>
                    <div className="dbg-filters" style={{ marginLeft: 'auto' }}>
                      {(['all', 'plugin', 'dashboard', 'other'] as const).map((f) => (
                        <button key={f} className={`dbg-fchip${filter === f ? ' on' : ''}`} onClick={() => setFilter(f)}>{f}</button>
                      ))}
                      <button className={`dbg-fchip${errorsOnly ? ' on' : ''}`} onClick={() => setErrorsOnly((v) => !v)}>errors</button>
                    </div>
                  </div>
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>time</th><th>lane</th><th>call</th><th>status</th><th className="dbg-right">latency</th></tr>
                      </thead>
                      <tbody>
                        {callsShown.map((g) => (
                          <tr key={`${g.key}-${g.ts}`} className={flashReq.has(g.ts) ? 'dbg-new' : ''}>
                            <td className="mono muted">{clock(g.ts)}</td>
                            <td><span className={`dbg-lane dbg-lane-${g.lane}`}>{g.lane}</span></td>
                            <td className="mono dbg-route">
                              {g.method === 'POST' ? <b className="dbg-post">POST</b> : 'GET'} {g.path}
                              {g.query ? <span className="muted">?{g.query.slice(0, 56)}</span> : null}
                              {g.n > 1 ? <> <span className="dbg-n">×{g.n}</span></> : null}
                              {g.sessions.size > 1 ? <> <span className="dbg-n">{g.sessions.size} ses</span></> : null}
                            </td>
                            <td className="mono" style={{ color: statusColor(g.status) }}>{g.status}</td>
                            <td>
                              <span className="dbg-barwrap">
                                <span className="dbg-bar" style={{ width: Math.min(56, (g.msMax / 300) * 56), background: latColor(g.msMax) }} />
                                <span className="mono muted">{Math.round(g.msMax)}ms</span>
                              </span>
                            </td>
                          </tr>
                        ))}
                        {callsShown.length === 0 && (
                          <tr><td colSpan={5} className="muted">no calls match — {err ? 'daemon unreachable' : 'nothing buffered yet'}</td></tr>
                        )}
                      </tbody>
                    </table>
                    {grouped.length > callsShown.length && (
                      <div style={{ padding: '8px 0 2px' }}>
                        <button className="dbg-btn" onClick={() => setCallsFull(true)}>show {grouped.length - callsShown.length} more</button>
                      </div>
                    )}
                    {callsFull && grouped.length > PEEK && (
                      <div style={{ padding: '8px 0 2px' }}>
                        <button className="dbg-btn" onClick={() => setCallsFull(false)}>collapse to {PEEK}</button>
                      </div>
                    )}
                  </div>
                </div>
              </section>

              {/* ---- lane injections ---- */}
              <section className="dbg-card" id="dbg-card-inj">
                <h5>
                  <span className="dbg-dot" style={{ background: 'var(--violet)' }} /> Lane injections
                  <span className="dbg-tag">{injs.length} buffered</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>time</th><th>lane</th><th>stage</th><th>chars</th><th>session</th><th>note</th></tr>
                      </thead>
                      <tbody>
                        {injShown.map((r, i) => (
                          <tr key={`${r.ts}-${i}`} className={flashInj.has(r.ts) ? 'dbg-new' : ''}>
                            <td className="mono muted">{clock(r.ts)}</td>
                            <td><span className="dbg-op" style={{ color: INJ_COLOR[r.lane] || 'var(--text-muted)' }}>{r.lane}</span></td>
                            <td className="mono muted">@{r.stage}</td>
                            <td className="mono">{r.chars}</td>
                            <td className="mono">{shortSid(r.session)}</td>
                            <td className="muted">{trunc(r.note, 40)}</td>
                          </tr>
                        ))}
                        {injShown.length === 0 && (
                          <tr><td colSpan={6} className="muted">nothing reported yet — needs the updated plugin (one OpenCode restart) and a new session.</td></tr>
                        )}
                      </tbody>
                    </table>
                    {injRows.length > injShown.length && (
                      <div style={{ padding: '8px 0 2px' }}>
                        <button className="dbg-btn" onClick={() => setInjFull(true)}>show {injRows.length - injShown.length} more</button>
                      </div>
                    )}
                    {injFull && injRows.length > PEEK && (
                      <div style={{ padding: '8px 0 2px' }}>
                        <button className="dbg-btn" onClick={() => setInjFull(false)}>collapse to {PEEK}</button>
                      </div>
                    )}
                  </div>
                </div>
              </section>

              {/* ---- mind ops ---- */}
              <section className="dbg-card" id="dbg-card-ops">
                <h5>
                  <span className="dbg-dot" style={{ background: 'var(--ok)' }} /> Mind ops
                  <span className="dbg-tag">{ov.counts.ops_today} today</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>when</th><th>op</th><th>what</th><th>session</th></tr>
                      </thead>
                      <tbody>
                        {ov.recent_ops.slice(0, 40).map((o, i) => (
                          <tr key={i}>
                            <td className="mono muted">{ago(o.ts)}</td>
                            <td><span className="dbg-op" style={{ color: OP_COLOR[o.op] || 'var(--text-muted)' }}>{o.op}</span></td>
                            <td className="dbg-sum" title={o.label || o.query || o.atom_id || ''}>{trunc(o.label || o.query || o.atom_id || '', 90)}</td>
                            <td className="mono muted">{shortSid(o.session_id)}</td>
                          </tr>
                        ))}
                        {ov.recent_ops.length === 0 && <tr><td colSpan={4} className="muted">no ops yet</td></tr>}
                      </tbody>
                    </table>
                  </div>
                  <p className="dbg-note">
                    today: {ov.ops_today.map((o) => `${o.op} ${o.n}`).join(' · ') || 'none'}<br />
                    states: {ov.rank_states.map((r) => `${r.state} ${r.n}`).join(' · ') || '—'} · types: {ov.atom_types.map((t) => `${t.type} ${t.n}`).join(' · ') || '—'}
                  </p>
                </div>
              </section>

              {/* ---- feedback trail ---- */}
              <section className="dbg-card" id="dbg-card-fb">
                <h5>
                  <span className="dbg-dot" style={{ background: 'var(--warn)' }} /> Feedback trail
                  <span className="dbg-tag">{ov.counts.feedback_24h} in 24h</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>when</th><th>atom</th><th>verdict</th><th>source</th><th>rater → origin</th></tr>
                      </thead>
                      <tbody>
                        {ov.feedback.slice(0, 30).map((f, i) => (
                          <tr key={i}>
                            <td className="mono muted">{ago(f.ts)}</td>
                            <td className="mono">{f.atom_id}</td>
                            <td style={{ color: f.signal === 'useful' ? 'var(--ok)' : f.signal === 'noise' ? 'var(--err)' : undefined }}>{f.signal}</td>
                            <td className="mono muted">{f.source}</td>
                            <td className="mono muted">{shortSid(f.session_id) || '—'}{f.origin_session_id ? ` → ${shortSid(f.origin_session_id)}` : ''}</td>
                          </tr>
                        ))}
                        {ov.feedback.length === 0 && <tr><td colSpan={5} className="muted">no verdicts yet</td></tr>}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>

              {/* ---- daemon ---- */}
              <section className="dbg-card" id="dbg-card-daemon">
                <h5>
                  <span className="dbg-dot" style={{ background: h.ribbon.daemon.level === 'ok' ? 'var(--ok)' : 'var(--err)' }} /> Daemon
                  <span className="dbg-tag">{ov.daemon.supervised ? 'supervised' : 'orphan'}</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-statgrid">
                    {stat('pid', ov.daemon.pid)}
                    {stat('uptime', fmtUptime(ov.daemon.uptime_s))}
                    {stat('started', ov.daemon.started_at ? clock(ov.daemon.started_at) : '—')}
                    {stat('life loop', ov.daemon.life_enabled ? 'on' : 'off')}
                  </div>
                  <p className="dbg-note">
                    unit {ov.daemon.service} · bind {ov.daemon.bind}<br />
                    {ov.daemon.supervised
                      ? `supervised by ${ov.daemon.manager} — Restart= brings it back after a crash`
                      : 'NOT supervised — an orphan is never restarted'}
                  </p>
                </div>
              </section>

              {/* ---- life loop ---- */}
              <section className="dbg-card" id="dbg-card-life">
                <h5>
                  <span className="dbg-dot" style={{ background: h.ribbon.life.level === 'ok' ? 'var(--ok)' : 'var(--warn)' }} /> Life loop
                  <span className="dbg-tag">{ov.quiet_now ? 'quiet hours' : 'awake'}</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>phase</th><th>every</th><th>last</th><th>next</th><th>result</th></tr>
                      </thead>
                      <tbody>
                        {ov.phases.map((p) => (
                          <tr key={p.phase}>
                            <td className="mono">{p.phase}</td>
                            <td className="muted">{p.every_minutes}m</td>
                            <td>{ago(p.last?.ts)}</td>
                            <td className="mono" style={{ color: p.last?.next_due && Date.now() > p.last.next_due ? 'var(--warn)' : 'var(--text-muted)' }}>{inMins(p.last?.next_due)}</td>
                            <td className="dbg-sum" title={p.last?.summary || ''}>{trunc(p.last?.summary, 52) || '—'}</td>
                          </tr>
                        ))}
                        <tr>
                          <td className="mono">dream</td>
                          <td className="muted">nightly</td>
                          <td>{ago(ov.last_dream?.ts)}</td>
                          <td className="mono muted">02:00</td>
                          <td className="dbg-sum" title={ov.last_dream?.label || ''}>{trunc(ov.last_dream?.label, 52) || '—'}</td>
                        </tr>
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>

              {/* ---- rating watch ---- */}
              <section className="dbg-card" id="dbg-card-rating">
                <h5>
                  <span className="dbg-dot" style={{ background: h.ribbon.rating.level === 'ok' ? 'var(--ok)' : h.ribbon.rating.level === 'warn' ? 'var(--warn)' : 'var(--text-muted)' }} /> Rating watch
                  <span className="dbg-tag">{ov.rating_watch.enabled ? `${String(ov.rating_watch.hour).padStart(2, '0')}:00` : 'off'}</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-statgrid">
                    {stat('useful', ov.rating_watch.current.useful)}
                    {stat('noise', ov.rating_watch.current.noise)}
                    {stat('used', ov.rating_watch.current.used)}
                    {stat('raters', ov.rating_watch.current.raters)}
                    {stat('unranked', `${ov.rating_watch.current.unranked}/${ov.rating_watch.current.atoms}`)}
                  </div>
                  <p className="dbg-note">
                    window {ov.rating_watch.days}d · last snapshot {ago(ov.rating_watch.last?.ts)} · next {inMins(ov.rating_watch.last?.next_due)}
                    {ov.rating_watch.until ? ` · stops after ${ov.rating_watch.until}` : ''}
                  </p>
                </div>
              </section>

              {/* ---- ingest runs ---- */}
              <section className="dbg-card" id="dbg-card-ingest">
                <h5>
                  <span className="dbg-dot" style={{ background: h.ribbon.ingest.level === 'ok' ? 'var(--ok)' : 'var(--warn)' }} /> Ingest runs
                  <span className="dbg-tag">{ov.ingest.length} runs</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <table className="dbg-table">
                      <thead>
                        <tr><th>source</th><th>started</th><th>took</th><th>added</th><th>updated</th><th>errors</th></tr>
                      </thead>
                      <tbody>
                        {ov.ingest.slice(0, 10).map((r, i) => (
                          <tr key={i}>
                            <td className="mono">{r.source}</td>
                            <td className="muted">{ago(r.started_at)}</td>
                            <td className="mono muted">{r.finished_at ? `${Math.max(0, Math.round((r.finished_at - r.started_at) / 100) / 10)}s` : '—'}</td>
                            <td className="mono">{r.added}</td>
                            <td className="mono">{r.updated}</td>
                            <td className="mono" style={{ color: r.errors ? 'var(--err)' : undefined }}>{r.errors}</td>
                          </tr>
                        ))}
                        {ov.ingest.length === 0 && <tr><td colSpan={6} className="muted">no ingest runs yet</td></tr>}
                      </tbody>
                    </table>
                  </div>
                </div>
              </section>

              {/* ---- self & soul ---- */}
              <section className="dbg-card" id="dbg-card-self">
                <h5>
                  <span className="dbg-dot" style={{ background: 'var(--violet)' }} /> Self &amp; soul
                  <span className="dbg-tag">doc versions · rules</span>
                </h5>
                <div className="dbg-body">
                  <div className="dbg-scroll" tabIndex={0}>
                    <div className="dbg-sub-head"><span className="dbg-lane">persona docs</span></div>
                    <div className="dbg-kv">
                      {ov.self_trace.doc_counts.length
                        ? ov.self_trace.doc_counts.map((d) => (
                            <div className="dbg-kvrow" key={d.doc_path}>
                              <span className="k">{d.doc_path.split('/').pop()}</span>
                              <span>{d.versions} v · {ago(d.last_ts)}</span>
                            </div>
                          ))
                        : <div className="dbg-kvrow muted">no versions yet</div>}
                    </div>
                    <div className="dbg-sub-head" style={{ marginTop: 11 }}><span className="dbg-lane">rules about you</span></div>
                    <div className="dbg-kv">
                      {ov.self_trace.preferences.length
                        ? ov.self_trace.preferences.map((p) => (
                            <div className="dbg-kvrow" key={p.id}>
                              <span className="muted">{ago(p.created)}</span>
                              <span style={{ textAlign: 'right' }}>{trunc(p.label, 60)}</span>
                            </div>
                          ))
                        : <div className="dbg-kvrow muted">no preference atoms</div>}
                    </div>
                  </div>
                </div>
              </section>
            </div>

            {/* ---- effective config (below the board; opening it grows the page) ---- */}
            <section className={`dbg-card dbg-collapsible${configOpen ? '' : ' closed'}`} id="dbg-card-config">
              <h5 onClick={() => setConfigOpen((v) => !v)}>
                <span className="dbg-dot" style={{ background: 'var(--text-muted)' }} /> Effective config
                <span className="dbg-spacer" /><span className="dbg-chev">▾</span>
              </h5>
              <div className="dbg-body">
                <div className="dbg-config">
                  {Object.entries(ov.config).map(([k, v]) => (
                    <div className="dbg-kvrow" key={k}>
                      <span className="k">{k}</span>
                      <span>{String(v)}</span>
                    </div>
                  ))}
                </div>
              </div>
            </section>
          </>
        )}
      </div>
    </div>
  )
}
