import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { BookOpen, Search, Sparkles, X } from 'lucide-react'
import { ATOM_COLORS, ATOM_LABELS } from './theme'
import { DocCards } from './DocCards'

interface Memory {
  id: string
  label: string
  type: string
  created: number
  text: string | null
  source: string | null
  hub: string | null
  tags: string[]
  weight: number
  origin: string
  // provenance + rank (m_atoms.session_id / m_rank / m_feedback)
  session_id: string | null
  quality: number
  state: string
  served: number
  interacted: number
  last_served: number | null
  used: number
  useful: number
  noise: number
  ranked: boolean
}

// how each memory got into the mind (matches mind._origin)
const ORIGIN_LABEL: Record<string, string> = {
  stored: 'stored',
  promoted: 'promoted',
  note: 'from note',
  rule: 'rule',
  dream: 'dream',
  identity: 'identity',
  memory: 'memory',
}
const ORIGIN_COLOR: Record<string, string> = {
  stored: '#34d399',
  promoted: '#a78bfa',
  note: '#8fa3b8',
  rule: '#948ebd',
  dream: '#a488b6',
  identity: '#c9a9c4',
  memory: '#8f96c4',
}

// links open outside the app and must not toggle the card
const MD_COMPONENTS = {
  a: ({ node, ...props }: any) => (
    <a {...props} target="_blank" rel="noopener noreferrer" onClick={(e: any) => e.stopPropagation()} />
  ),
} as any

const fmtTime = (ms: number) =>
  new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' })

function dayLabel(ms: number): string {
  const d = new Date(ms)
  const start = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const diff = Math.round((start(new Date()) - start(d)) / 86_400_000)
  if (diff === 0) return 'Today'
  if (diff === 1) return 'Yesterday'
  return d.toLocaleDateString(undefined, {
    weekday: 'short', month: 'short', day: '2-digit',
    ...(d.getFullYear() === new Date().getFullYear() ? {} : { year: 'numeric' }),
  })
}

const fmtDay = (ms: number) =>
  new Date(ms).toLocaleDateString(undefined, { month: 'short', day: '2-digit' })

// "last used" — when the memory was last served to an agent (m_rank.last_served).
const sinceLabel = (ms?: number | null): string => {
  if (!ms) return 'never'
  const s = Math.max(0, Math.round((Date.now() - ms) / 1000))
  if (s < 60) return 'just now'
  const m = Math.round(s / 60)
  if (m < 60) return `${m}m ago`
  const h = Math.round(m / 60)
  if (h < 48) return `${h}h ago`
  return `${Math.round(h / 24)}d ago`
}

function MemoryCard({ m, onOpen }: { m: Memory; onOpen: (id: string) => void }) {
  const [expanded, setExpanded] = useState(false)
  const [overflowing, setOverflowing] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const text = m.text ?? ''

  // is the memo taller than the collapsed box? (measure only while collapsed)
  useEffect(() => {
    if (expanded) return
    const el = bodyRef.current
    if (el) setOverflowing(el.scrollHeight > el.clientHeight + 4)
  }, [text, expanded])

  // a stored atom's label is just the start of its text — don't repeat it
  const showLabel = !!m.label && !text.startsWith(m.label)
  const clickable = overflowing && !expanded

  return (
    <article className={'mc' + (clickable ? ' clickable' : '')} onClick={clickable ? () => setExpanded(true) : undefined}>
      <div className="mc-top">
        <span className="mc-dot" style={{ background: ATOM_COLORS[m.type] ?? '#8f96c4' }} />
        <span className="mc-type" style={{ color: ATOM_COLORS[m.type] ?? 'var(--text-dim)' }}>
          {ATOM_LABELS[m.type] ?? m.type}
        </span>
        {m.hub && <span className="mc-hub">{m.hub}</span>}
        <div style={{ flex: 1 }} />
        <span className="mc-origin" style={{ color: ORIGIN_COLOR[m.origin] ?? 'var(--text-muted)' }}>
          {ORIGIN_LABEL[m.origin] ?? m.origin}
        </span>
        <span className="mc-time mono">{fmtTime(m.created)}</span>
      </div>
      {showLabel && <h3 className="mc-label">{m.label}</h3>}
      {text && (
        <>
          <div ref={bodyRef} className={'mc-body' + (expanded ? '' : ' clamp')}>
            <div className="md">
              <ReactMarkdown remarkPlugins={[remarkGfm]} components={MD_COMPONENTS}>{text}</ReactMarkdown>
            </div>
          </div>
          {overflowing && !expanded && <div className="mc-fade" />}
        </>
      )}
      <div className="mc-foot">
        {/* rank: state + quality, or an explicit "unranked" when no signal exists */}
        <span
          className={'mc-rank' + (m.ranked ? '' : ' unranked') + (m.state !== 'active' ? ' ' + m.state : '')}
          title={`quality ${m.quality.toFixed(2)} · served ${m.served} · interacted ${m.interacted} · useful ${m.useful} / noise ${m.noise}`}
        >
          {m.ranked ? `${m.state} · ${m.quality.toFixed(2)}` : 'unranked'}
        </span>
        <span className="mc-used mono" title={m.last_served ? new Date(m.last_served).toLocaleString() : 'never served'}>
          used {sinceLabel(m.last_served)}
        </span>
        {m.session_id && (
          <span className="mc-sid mono" title={`saved from ${m.session_id}`}>
            from {m.session_id.replace(/^ses_/, '').slice(0, 10)}
          </span>
        )}
        {m.source && <span className="mc-src mono">{m.source}</span>}
        {m.tags.map((t) => <span className="obs-tag" key={t}>#{t}</span>)}
        <div style={{ flex: 1 }} />
        {overflowing && (
          <button className="obs-btn" onClick={(e) => { e.stopPropagation(); setExpanded((v) => !v) }}>
            {expanded ? 'Less' : 'More'}
          </button>
        )}
        <button className="obs-btn" onClick={(e) => { e.stopPropagation(); onOpen(m.id) }}>
          <Sparkles size={12} /> Open in Brain
        </button>
      </div>
    </article>
  )
}

export function MemoryView({ onOpen }: { onOpen: (id: string) => void }) {
  const [mode, setMode] = useState<'memories' | 'notes'>('memories')
  const [items, setItems] = useState<Memory[]>([])
  const [counts, setCounts] = useState<Record<string, number>>({})
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [q, setQ] = useState('')
  const [type, setType] = useState<string | null>(null)

  const load = useCallback(() => {
    fetch('/api/mind/memories?limit=1000')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
      .then((d) => {
        setItems(d.items ?? [])
        setCounts(d.counts ?? {})
        setTotal(d.total ?? 0)
        setLoading(false)
      })
      .catch(() => setLoading(false))
  }, [])
  useEffect(() => {
    load()
    const t = window.setInterval(load, 30_000)
    return () => window.clearInterval(t)
  }, [load])

  const filtered = useMemo(() => {
    const needle = q.trim().toLowerCase()
    return items.filter((m) => {
      if (type && m.type !== type) return false
      if (!needle) return true
      const hay = `${m.label} ${m.text ?? ''} ${(m.tags ?? []).join(' ')} ${m.hub ?? ''} ${m.source ?? ''}`
      return hay.toLowerCase().includes(needle)
    })
  }, [items, q, type])

  // items arrive newest-first, so grouping preserves the order
  const groups = useMemo(() => {
    const map = new Map<string, Memory[]>()
    for (const m of filtered) {
      const k = new Date(m.created).toDateString()
      const arr = map.get(k)
      if (arr) arr.push(m)
      else map.set(k, [m])
    }
    return [...map.values()]
  }, [filtered])

  const types = Object.keys(ATOM_LABELS).filter((t) => (counts[t] ?? 0) > 0)

  return (
    <div className="dreams mem-page">
      <header className="dreams-head">
        <h1><BookOpen size={18} style={{ verticalAlign: -3, marginRight: 8, color: 'var(--text-muted)' }} />Memory</h1>
        <p className="muted">
          What the agent has memorized — every memory in the mind store, newest first. The raw daily
          notes it was written into are under <b>Daily notes</b>.
        </p>
      </header>

      <div className="seg" role="tablist" aria-label="Memory views">
        <button role="tab" aria-selected={mode === 'memories'} className={mode === 'memories' ? 'on' : ''} onClick={() => setMode('memories')}>
          Memories <span className="cnt">{total}</span>
        </button>
        <button role="tab" aria-selected={mode === 'notes'} className={mode === 'notes' ? 'on' : ''} onClick={() => setMode('notes')}>
          Daily notes
        </button>
      </div>

      {mode === 'notes' ? (
        <DocCards group="memory" onOpen={onOpen} />
      ) : (
        <>
          <div className="mem-tools">
            <div className="mem-search">
              <Search size={14} />
              <input value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search memorized text, tags, hubs…" spellCheck={false} />
              {q && <button className="x" onClick={() => setQ('')} aria-label="Clear search"><X size={13} /></button>}
            </div>
            <div className="mem-chips">
              <button className={'chip' + (type === null ? ' on' : '')} onClick={() => setType(null)}>
                All<span className="n">{total}</span>
              </button>
              {types.map((t) => (
                <button
                  key={t}
                  className={'chip' + (type === t ? ' on' : '')}
                  style={type === t ? { borderColor: ATOM_COLORS[t], color: ATOM_COLORS[t] } : undefined}
                  onClick={() => setType(type === t ? null : t)}
                >
                  <span className="dot" style={{ background: ATOM_COLORS[t] }} />
                  {ATOM_LABELS[t]}<span className="n">{counts[t]}</span>
                </button>
              ))}
            </div>
          </div>

          {loading && <div className="muted" style={{ padding: 20 }}>loading…</div>}
          {!loading && filtered.length === 0 && (
            <div className="muted" style={{ padding: 20 }}>No memories match.</div>
          )}

          <div className="mem-groups">
            {groups.map((arr) => (
              <section className="mem-day" key={new Date(arr[0].created).toDateString()}>
                <div className="mem-day-head">
                  {dayLabel(arr[0].created)}
                  <span className="sub mono">{fmtDay(arr[0].created)}</span>
                  <span className="n">{arr.length}</span>
                </div>
                {arr.map((m) => <MemoryCard key={m.id} m={m} onOpen={onOpen} />)}
              </section>
            ))}
          </div>

          {!loading && filtered.length > 0 && (
            <div className="mem-more muted">
              {filtered.length === total ? `${total} memories` : `${filtered.length} of ${total} memories`}
            </div>
          )}
        </>
      )}
    </div>
  )
}
