import { useEffect, useState } from 'react'
import { Bug } from 'lucide-react'

// Debug page (SPEC §5 visibility): one clean view of what the brain and the
// plugin actually did — no journal diving required. Everything here comes from
// the daemon's own records: /api/debug/overview (DB aggregates + life-loop
// state) and /api/debug/requests (rolling trace of every /api call).

type Req = {
  ts: number
  method: string
  path: string
  query: string
  status: number
  ms: number
  session: string
}

type Overview = {
  now: number
  counts: Record<string, number>
  rank_states: { state: string; n: number }[]
  atom_types: { type: string; n: number }[]
  ops_today: { op: string; n: number }[]
  recent_ops: { ts: number; op: string; atom_id: string | null; label: string | null; query: string | null; session_id: string | null }[]
  feedback: { ts: number; atom_id: string; signal: string; source: string; session_id: string | null }[]
  ingest: { started_at: number; finished_at: number | null; source: string; added: number; updated: number; errors: number; note: string | null }[]
  phases: { phase: string; every_minutes: number; last: { ts: number; summary: string; next_due: number } | null }[]
  quiet_now: boolean
  last_dream: { ts: number; label: string } | null
  config: Record<string, string | number | boolean>
}

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

const OP_COLOR: Record<string, string> = {
  store: '#7fd18a',
  recall: '#8fb3ff',
  feedback: '#e8c66b',
  dream: '#c39be0',
  link: '#6ecfc4',
  forget: '#e08585',
}

const statusColor = (s: number) => (s < 300 ? '#7fd18a' : s < 400 ? 'var(--text-muted)' : '#e08585')

// Which producer made this call — the OpenCode plugin (wakeup/recall/feedback),
// the dashboard itself (graph/activity polling), or something else (CLI, tests).
const laneTag = (r: Req) =>
  r.path.startsWith('/api/mind/wakeup') || r.path.startsWith('/api/mind/recall') || r.path === '/api/mind/feedback'
    ? 'plugin'
    : r.path.startsWith('/api/mind/activity') || r.path.startsWith('/api/mind/graph')
      ? 'dashboard'
      : r.path.startsWith('/api/debug')
        ? 'debug'
        : 'other'

export function DebugView() {
  const [ov, setOv] = useState<Overview | null>(null)
  const [reqs, setReqs] = useState<Req[]>([])
  const [err, setErr] = useState(false)

  useEffect(() => {
    let alive = true
    const load = () => {
      fetch('/api/debug/requests?limit=200')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
        .then((d) => alive && setReqs(d.requests ?? []))
        .catch(() => alive && setErr(true))
      fetch('/api/debug/overview')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
        .then((d) => {
          if (!alive) return
          setOv(d)
          setErr(false)
        })
        .catch(() => alive && setErr(true))
    }
    load()
    const t = window.setInterval(load, 5000)
    return () => {
      alive = false
      window.clearInterval(t)
    }
  }, [])

  const stat = (label: string, value: string | number) => (
    <div className="dbg-stat" key={label}>
      <div className="dbg-stat-v">{value}</div>
      <div className="dbg-stat-l">{label}</div>
    </div>
  )

  return (
    <div className="debug">
      <header className="debug-head">
        <div>
          <h1>
            <Bug size={18} style={{ verticalAlign: -3, marginRight: 8, color: 'var(--text-muted)' }} />
            Debug
          </h1>
          <p className="muted">
            What the brain and the plugin actually did — live, refreshed every 5s.
            {' '}«plugin» rows are the OpenCode lanes (wakeup / recall / feedback) hitting the mind API.
          </p>
        </div>
        <span className={`flag${err ? '' : ' on'}`}>{err ? 'daemon unreachable' : 'live · 5s'}</span>
      </header>

      {!ov && <div className="muted" style={{ padding: 20 }}>loading…</div>}

      {ov && (
        <div className="debug-body">
          {/* ---- at a glance ---- */}
          <div className="dbg-stats">
            {stat('atoms', ov.counts.atoms)}
            {stat('hubs', ov.counts.hubs)}
            {stat('links', ov.counts.edges)}
            {stat('mind ops today', ov.counts.ops_today)}
            {stat('feedback 24h', ov.counts.feedback_24h)}
            {stat('atoms by state', ov.rank_states.map((r) => `${r.state} ${r.n}`).join(' · ') || '—')}
          </div>

          {/* ---- life loop ---- */}
          <section className="dbg-card">
            <h5>Life loop {ov.quiet_now ? <span className="flag">quiet hours — only perceive</span> : <span className="flag on">awake</span>}</h5>
            <table className="dbg-table">
              <thead>
                <tr><th>phase</th><th>every</th><th>last run</th><th>next due</th><th>last result</th></tr>
              </thead>
              <tbody>
                {ov.phases.map((p) => (
                  <tr key={p.phase}>
                    <td className="mono">{p.phase}</td>
                    <td>{p.every_minutes}m</td>
                    <td>{ago(p.last?.ts)}</td>
                    <td>{inMins(p.last?.next_due)}</td>
                    <td className="dbg-summary">{p.last?.summary || '—'}</td>
                  </tr>
                ))}
                <tr>
                  <td className="mono">rest / dream</td>
                  <td>nightly</td>
                  <td>{ago(ov.last_dream?.ts)}</td>
                  <td>02:00</td>
                  <td className="dbg-summary">last: {ov.last_dream?.label || '—'}</td>
                </tr>
              </tbody>
            </table>
          </section>

          {/* ---- live trace ---- */}
          <section className="dbg-card">
            <h5>Live request trace — newest first ({reqs.length})</h5>
            <div className="dbg-scroll">
              <table className="dbg-table">
                <thead>
                  <tr><th>time</th><th>lane</th><th>call</th><th>session</th><th>status</th><th>ms</th></tr>
                </thead>
                <tbody>
                  {[...reqs].reverse().map((r, i) => (
                    <tr key={`${r.ts}-${i}`}>
                      <td className="mono">{clock(r.ts)}</td>
                      <td>
                        <span className={`dbg-lane dbg-lane-${laneTag(r)}`}>{laneTag(r)}</span>
                      </td>
                      <td className="mono dbg-route">
                        {r.method === 'POST' ? <b className="dbg-post">POST</b> : 'GET'} {r.path}
                        {r.query ? <span className="muted">?{r.query.slice(0, 70)}</span> : null}
                      </td>
                      <td className="mono">{shortSid(r.session)}</td>
                      <td className="mono" style={{ color: statusColor(r.status) }}>{r.status}</td>
                      <td className="mono">{r.ms}</td>
                    </tr>
                  ))}
                  {reqs.length === 0 && (
                    <tr><td colSpan={6} className="muted">no calls since the daemon restarted — open a new OpenCode session or use the brain to see the plugin lanes fire.</td></tr>
                  )}
                </tbody>
              </table>
            </div>
          </section>

          {/* ---- mind op log + feedback ---- */}
          <div className="dbg-cols">
            <section className="dbg-card">
              <h5>Recent mind ops</h5>
              <div className="dbg-scroll">
                <table className="dbg-table">
                  <thead>
                    <tr><th>when</th><th>op</th><th>what</th><th>session</th></tr>
                  </thead>
                  <tbody>
                    {ov.recent_ops.map((o, i) => (
                      <tr key={i}>
                        <td className="mono">{ago(o.ts)}</td>
                        <td><span className="dbg-op" style={{ color: OP_COLOR[o.op] || 'var(--text-muted)' }}>{o.op}</span></td>
                        <td className="dbg-summary">{o.label || o.query || o.atom_id || ''}</td>
                        <td className="mono">{shortSid(o.session_id)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <p className="muted dbg-note">today: {ov.ops_today.map((o) => `${o.op} ${o.n}`).join(' · ') || 'none'}</p>
            </section>

            <section className="dbg-card">
              <h5>Feedback trail (quality signals)</h5>
              <div className="dbg-scroll">
                <table className="dbg-table">
                  <thead>
                    <tr><th>when</th><th>atom</th><th>verdict</th><th>source</th></tr>
                  </thead>
                  <tbody>
                    {ov.feedback.map((f, i) => (
                      <tr key={i}>
                        <td className="mono">{ago(f.ts)}</td>
                        <td className="mono">{f.atom_id}</td>
                        <td style={{ color: f.signal === 'useful' ? '#7fd18a' : f.signal === 'noise' ? '#e08585' : 'var(--text)' }}>{f.signal}</td>
                        <td className="mono">{f.source}</td>
                      </tr>
                    ))}
                    {ov.feedback.length === 0 && <tr><td colSpan={4} className="muted">no verdicts yet</td></tr>}
                  </tbody>
                </table>
              </div>
            </section>
          </div>

          {/* ---- ingest ---- */}
          <section className="dbg-card">
            <h5>Ingest runs (last {ov.ingest.length})</h5>
            <div className="dbg-scroll">
              <table className="dbg-table">
                <thead>
                  <tr><th>source</th><th>started</th><th>took</th><th>added</th><th>updated</th><th>errors</th></tr>
                </thead>
                <tbody>
                  {ov.ingest.map((r, i) => (
                    <tr key={i}>
                      <td className="mono">{r.source}</td>
                      <td>{ago(r.started_at)}</td>
                      <td className="mono">{r.finished_at ? `${Math.max(0, Math.round((r.finished_at - r.started_at) / 100) / 10)}s` : '—'}</td>
                      <td className="mono">{r.added}</td>
                      <td className="mono">{r.updated}</td>
                      <td className="mono" style={{ color: r.errors ? '#e08585' : undefined }}>{r.errors}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          </section>

          {/* ---- config ---- */}
          <section className="dbg-card">
            <h5>Effective config</h5>
            <div className="dbg-config">
              {Object.entries(ov.config).map(([k, v]) => (
                <div className="dbg-config-row" key={k}>
                  <span className="mono">{k}</span>
                  <span>{String(v)}</span>
                </div>
              ))}
            </div>
          </section>
        </div>
      )}
    </div>
  )
}
