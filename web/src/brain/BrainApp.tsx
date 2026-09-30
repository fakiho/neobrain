import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Menu, PanelLeft, Pause, Play, Radio, RotateCcw, Search, Check, Minus, X, ChevronDown, ChevronUp } from 'lucide-react'
import type { BrainLink, BrainNode } from './types'
import { BrainCanvas } from './BrainCanvas'
import { DreamsView } from './DreamsView'
import { MemoryView } from './MemoryView'
import { ReaderView, type ReaderGroup } from './ReaderView'
import { useIsMobile } from './useMedia'
import { ATOM_COLORS, ATOM_LABELS, EDGE_COLORS, EDGE_LABELS, HUB_COLOR } from './theme'

const TABS = [
  ['brain', 'Brain'],
  ['dreams', 'Dreams'],
  ['memory', 'Memory'],
  ['identity', 'Identity'],
  ['infra', 'Infra'],
] as const

const BRAND_MARK = (
  <svg width="15" height="15" viewBox="0 0 24 24" fill="none">
    <circle cx="12" cy="12" r="3" fill="#c8cbe0" />
    <circle cx="5" cy="6" r="1.8" fill="#8f96c4" /><circle cx="19" cy="7" r="1.8" fill="#8f96c4" />
    <circle cx="6" cy="18" r="1.8" fill="#8f96c4" /><circle cx="18" cy="17" r="1.8" fill="#8f96c4" />
    <path d="M12 12 5 6M12 12l7-5M12 12l-6 6M12 12l6 5" stroke="#3a4152" strokeWidth="1" />
  </svg>
)

const fmt = (ms: number) =>
  new Date(ms).toLocaleString(undefined, { month: 'short', day: '2-digit', hour: '2-digit', minute: '2-digit' })
const fmtTime = (ms: number) => new Date(ms).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit', second: '2-digit' })

const REPLAY_MS = 20000
const relColor = (t: string) => (t === 'about' ? 'var(--text-muted)' : EDGE_COLORS[t] ?? 'var(--text-muted)')

// templates for simulated stores (stand-ins for real remember() calls)
const STORE_TEMPLATES = [
  { type: 'observation', hub: 'openwrt', label: 'Conntrack at 4.2k', text: 'Conntrack table steady at ~4,200 entries after the nft DNS enforcement went live. No drops.', tags: ['conntrack', 'openwrt'] },
  { type: 'decision', hub: 'dns', label: 'Keep DoH blocked', text: 'Keep DoH/DoT blocked and logged; IoT devices fall back to AdGuard cleanly, no user complaints.', tags: ['dns', 'doh'] },
  { type: 'lesson', hub: 'frigate', label: 'Cap beats resolution', text: 'Capping HomeKit bitrate is more effective than lowering resolution — corruption comes from UDP loss, not pixels.', tags: ['frigate', 'homekit'] },
  { type: 'observation', hub: 'grafana', label: 'device_inventory stable', text: 'device_inventory now shows ~40 devices with names; enrichment via mDNS + AdGuard PTR.', tags: ['grafana', 'inventory'] },
  { type: 'preference', hub: 'agent', label: 'Cite the source', text: 'When memorizing a decision, always record the source (file or session) so it stays traceable.', tags: ['agent', 'rule'] },
  { type: 'decision', hub: 'litellm', label: 'Stay pinned', text: 'Stay on litellm v1.101.2 until the unmapped-model regression is fixed upstream.', tags: ['litellm'] },
]
const RECALL_QUERIES = [
  'what do I know about DNS enforcement?',
  'why did Matter break?',
  'how are the cameras wired?',
  'what is the DHCP reservation gotcha?',
  'how is OpenCode exposed?',
  'latest on litellm pinning',
  'what changed on the host network?',
]

export default function BrainApp() {
  // the graph starts empty and fills from the real mind API (/api/mind/graph)
  const [nodes, setNodes] = useState<BrainNode[]>([])
  const [links, setLinks] = useState<BrainLink[]>([])
  const [mode, setMode] = useState<'prototype' | 'real'>('prototype')
  const [realOps, setRealOps] = useState(false)
  const [selected, setSelected] = useState<string | null>(null)
  const [search, setSearch] = useState('')
  const [leftOpen, setLeftOpen] = useState(false)
  const [fitSignal, setFitSignal] = useState(0)
  const [live, setLive] = useState(true)
  const [view, setView] = useState<'brain' | 'dreams' | ReaderGroup>('brain')
  const [hiddenTypes, setHiddenTypes] = useState<Set<string>>(new Set(['preference']))
  const [hiddenHubs, setHiddenHubs] = useState<Set<string>>(new Set())
  const [hiddenEdges, setHiddenEdges] = useState<Set<string>>(new Set())
  const [signals, setSignals] = useState(true)

  // ---- responsive shell (additive; desktop path is unchanged) ----
  const isMobile = useIsMobile()
  const [mobileDrawer, setMobileDrawer] = useState(false)
  const [searchOpen, setSearchOpen] = useState(false)
  const [feedOpen, setFeedOpen] = useState(!isMobile)
  useEffect(() => { setFeedOpen(!isMobile) }, [isMobile])

  // Measure the (taller, two-row) mobile header so the tab pages sit below it
  // instead of under it, whatever the tab strip does (1 or 2 rows, search open).
  const rootRef = useRef<HTMLDivElement>(null)
  const headerRef = useRef<HTMLElement>(null)
  const tabsRef = useRef<HTMLDivElement>(null)
  const [headerH, setHeaderH] = useState(0)
  useEffect(() => {
    const el = headerRef.current
    if (!isMobile || !el) { setHeaderH(0); return }
    const apply = () => {
      const h = Math.round(el.getBoundingClientRect().height)
      setHeaderH(h)
      rootRef.current?.style.setProperty('--m-header', `${h}px`)
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    return () => ro.disconnect()
  }, [isMobile])
  // re-fit the brain once the real header height is known
  useEffect(() => { if (isMobile && headerH) setFitSignal((n) => n + 1) }, [headerH, isMobile])
  // keep the active tab in view in the horizontally scrollable mobile strip
  useEffect(() => {
    if (!isMobile) return
    tabsRef.current?.querySelector('button.on')?.scrollIntoView({ inline: 'center', block: 'nearest', behavior: 'smooth' })
  }, [view, isMobile])

  // swipe-down to dismiss the mobile bottom sheet
  const sheetRef = useRef<HTMLElement | null>(null)
  const sheetDragY = useRef(0)
  const sheetStartY = useRef(0)
  const sheetDragging = useRef(false)
  const onSheetStart = (e: React.TouchEvent) => {
    if (!isMobile) return
    sheetStartY.current = e.touches[0].clientY
    sheetDragging.current = true
    if (sheetRef.current) sheetRef.current.style.transition = 'none'
  }
  const onSheetMove = (e: React.TouchEvent) => {
    if (!isMobile || !sheetDragging.current) return
    const dy = e.touches[0].clientY - sheetStartY.current
    if (dy > 0) {
      sheetDragY.current = dy
      if (sheetRef.current) sheetRef.current.style.transform = `translateY(${dy}px)`
    }
  }
  const onSheetEnd = () => {
    if (!isMobile || !sheetDragging.current) return
    sheetDragging.current = false
    const el = sheetRef.current
    if (!el) return
    el.style.transition = 'transform .22s ease'
    if (sheetDragY.current > 110) setSelected(null)
    else el.style.transform = 'translateY(0)'
    sheetDragY.current = 0
  }

  const [ops, setOps] = useState<{ op: string; id: string | null; label: string; ts: number; query?: string }[]>([])
  const [flashTs, setFlashTs] = useState<number | null>(null)
  const flashTimer = useRef<number | null>(null)
  const [recalls, setRecalls] = useState<Record<string, { n: number; last: number }>>({})
  const [pulse, setPulse] = useState<{ ids: string[]; at: number } | null>(null)

  const nodesRef = useRef(nodes)
  const linksRef = useRef(links)
  nodesRef.current = nodes
  linksRef.current = links
  const timeEndRef = useRef(0)
  const maxTRef = useRef(0)
  const tplRef = useRef(0)
  const liveIdRef = useRef(1)

  // ---- real mind data (M1) ----
  // Refetchable so the graph grows as the agent stores/links/forgets memories;
  // a mount-only fetch left freshly stored atoms missing client-side, so the
  // STORE rows in the live feed pointed at nodes the UI did not have.
  const loadGraph = useCallback(() => {
    fetch('/api/mind/graph')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((g) => {
        if (!g?.nodes?.length) return
        setNodes(g.nodes)
        setLinks(g.links)
        setMode('real')
      })
      .catch(() => {})
  }, [])

  useEffect(() => { loadGraph() }, [loadGraph])

  // poll real memory ops; if any exist, show those instead of the simulator
  useEffect(() => {
    let stop = false
    let lastOpTs = 0
    const load = () => {
      fetch('/api/mind/activity')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
        .then((d) => {
          if (stop || !d?.ops?.length) return
          const rows = d.ops.map((o: any) => ({ op: o.op, id: o.id, label: o.label, ts: o.ts, query: o.query }))
          setOps(rows)
          setRealOps(true)
          // freshly recorded ops: ping the graph node and flash the feed row 2.5 s
          const fresh = lastOpTs ? rows.filter((o: any) => o.ts > lastOpTs) : []
          if (fresh.length) {
            const ids = fresh.map((o: any) => o.id).filter(Boolean)
            if (ids.length) setPulse({ ids, at: performance.now() })
            setFlashTs(fresh.reduce((a: any, b: any) => (b.ts > a.ts ? b : a)).ts)
            if (flashTimer.current) window.clearTimeout(flashTimer.current)
            flashTimer.current = window.setTimeout(() => setFlashTs(null), 2500)
          }
          // Stores/links/forgets mutate the graph itself; pull it again so new
          // atoms exist and tapping their feed row opens a populated panel.
          const mutated = d.ops.some((o: any) => o.ts > lastOpTs && o.op !== 'recall')
          lastOpTs = d.ops.reduce((m: number, o: any) => Math.max(m, o.ts ?? 0), lastOpTs)
          if (mutated) loadGraph()
        })
        .catch(() => {})
    }
    load()
    const t = window.setInterval(load, 5000)
    return () => { stop = true; window.clearInterval(t) }
  }, [loadGraph])

  // ---- simulated live feed (stands in for the memory service stream) ----
  useEffect(() => {
    if (!live || realOps) return
    const tick = () => {
      const atoms = nodesRef.current.filter((n) => n.kind === 'atom')
      if (Math.random() < 0.4) {
        // store
        const tpl = STORE_TEMPLATES[tplRef.current++ % STORE_TEMPLATES.length]
        const id = `live_${liveIdRef.current++}`
        const created = Date.now()
        const node: BrainNode = {
          id, label: tpl.label, kind: 'atom', type: tpl.type, created,
          text: tpl.text, hub: tpl.hub, weight: 0.7, source: `session live ${fmtTime(created)}`, tags: tpl.tags,
        }
        const anchor = atoms[atoms.length - 1]
        const newLinks: BrainLink[] = [{ source: id, target: tpl.hub, type: 'about' }]
        if (anchor) newLinks.push({ source: id, target: anchor.id, type: 'derived-from' })
        setNodes((prev) => [...prev, node])
        setLinks((prev) => [...prev, ...newLinks])
        // only follow "now" if we were already at the present (don't yank the slider)
        if (timeEndRef.current >= maxTRef.current - 2000) setTimeEnd(created)
        setOps((prev) => [{ op: 'store', id, label: tpl.label, ts: created }, ...prev].slice(0, 60))
      } else if (atoms.length) {
        // recall
        const pick = atoms[Math.floor(Math.random() * atoms.length)]
        const query = RECALL_QUERIES[Math.floor(Math.random() * RECALL_QUERIES.length)]
        const now = Date.now()
        setPulse({ ids: [pick.id], at: performance.now() })
        setRecalls((prev) => ({ ...prev, [pick.id]: { n: (prev[pick.id]?.n ?? 0) + 1, last: now } }))
        setOps((prev) => [{ op: 'recall', id: pick.id, label: pick.label, ts: now, query }, ...prev].slice(0, 60))
      }
    }
    const timer = window.setInterval(tick, 2600)
    return () => window.clearInterval(timer)
  }, [live, realOps])

  const times = useMemo(() => nodes.map((n) => n.created).sort((a, b) => a - b), [nodes])
  const minT = times[0]
  const maxT = times[times.length - 1]
  const [timeEnd, setTimeEnd] = useState(maxT)
  timeEndRef.current = timeEnd
  maxTRef.current = maxT

  // when the data set changes (e.g. the first real graph, or a live store), keep the
  // present in view if we were already at the present
  const prevMaxRef = useRef(-1)
  useEffect(() => {
    const prev = prevMaxRef.current
    prevMaxRef.current = maxT
    setTimeEnd((t) => (prev < 0 || t >= prev - 1500 ? maxT : Math.min(t, maxT)))
  }, [maxT])
  const [playing, setPlaying] = useState(false)
  const progress = useRef(1)
  const raf = useRef<number | null>(null)

  useEffect(() => {
    if (!playing) return
    let last = performance.now()
    const step = (now: number) => {
      const dt = now - last
      last = now
      progress.current = Math.min(1, progress.current + dt / REPLAY_MS)
      const i = Math.min(times.length - 1, Math.floor(progress.current * (times.length - 1)))
      setTimeEnd(times[i])
      if (progress.current >= 1) { setPlaying(false); setTimeEnd(maxT); return }
      raf.current = requestAnimationFrame(step)
    }
    raf.current = requestAnimationFrame(step)
    return () => { if (raf.current) cancelAnimationFrame(raf.current) }
  }, [playing, times, maxT])

  const togglePlay = () => { if (!playing && progress.current >= 1) progress.current = 0; setPlaying((p) => !p) }
  const replay = () => { progress.current = 0; setTimeEnd(minT); setSelected(null); setPlaying(true) }

  const sel = selected ? nodes.find((n) => n.id === selected) ?? null : null

  const connections = useMemo(() => {
    if (!selected) return [] as { dir: 'out' | 'in'; type: string; node: BrainNode }[]
    const byId = new Map(nodes.map((n) => [n.id, n]))
    const out: { dir: 'out' | 'in'; type: string; node: BrainNode }[] = []
    for (const l of links) {
      if (l.source === selected && byId.has(l.target)) out.push({ dir: 'out', type: l.type, node: byId.get(l.target)! })
      if (l.target === selected && byId.has(l.source)) out.push({ dir: 'in', type: l.type, node: byId.get(l.source)! })
    }
    return out
  }, [selected, nodes, links])

  const matches = useMemo(() => {
    const q = search.trim().toLowerCase()
    if (q.length < 2) return []
    return nodes.filter((n) => n.label.toLowerCase().includes(q) || (n.text ?? '').toLowerCase().includes(q)).slice(0, 8)
  }, [search, nodes])

  const hubList = useMemo(() => {
    const counts: Record<string, number> = {}
    for (const l of links) if (l.type === 'about' && typeof l.target === 'string') counts[l.target] = (counts[l.target] ?? 0) + 1
    return nodes
      .filter((n) => n.kind === 'hub')
      .map((h) => ({ id: h.id, label: h.label, count: counts[h.id] ?? 0 }))
      .sort((a, b) => b.count - a.count || a.label.localeCompare(b.label))
  }, [nodes, links])

  const toggle = (setter: (fn: (p: Set<string>) => Set<string>) => void, key: string) =>
    setter((prev) => {
      const n = new Set(prev)
      n.has(key) ? n.delete(key) : n.add(key)
      return n
    })

  const hidden = useMemo(() => ({ types: hiddenTypes, hubs: hiddenHubs, edges: hiddenEdges }), [hiddenTypes, hiddenHubs, hiddenEdges])
  const edgeCounts = useMemo(() => {
    const c: Record<string, number> = {}
    for (const l of links) c[l.type] = (c[l.type] || 0) + 1
    return c
  }, [links])

  // when an identity doc is selected, reveal its rules (even if the Rules type is off)
  const showIds = useMemo(() => {
    const s = new Set<string>()
    if (!selected) return s
    if (nodes.find((n) => n.id === selected)?.type === 'identity') {
      for (const l of links) if (l.type === 'derived-from' && l.source === selected && typeof l.target === 'string') s.add(l.target)
    }
    return s
  }, [selected, nodes, links])

  const visibleCount = nodes.filter(
    (n) =>
      n.created <= timeEnd &&
      (showIds.has(n.id) ||
        !(n.kind === 'hub' ? hiddenHubs.has(n.id) : hiddenTypes.has(n.type) || (!!n.hub && hiddenHubs.has(n.hub)))),
  ).length
  const pct = ((timeEnd - minT) / Math.max(1, maxT - minT)) * 100
  const recallInfo = selected ? recalls[selected] : undefined

  // The legend + live feed panel. Rendered as the desktop left rail, and as the
  // slide-in drawer on mobile (filters only there; the feed moves to a bottom strip).
  const activityRows = (
    <>
      {ops.length === 0 && <div className="muted" style={{ fontSize: 12 }}>waiting…</div>}
      {ops.map((o, i) => {
        const badge: Record<string, string> = { store: 'STORE', recall: 'RECALL', link: 'LINK', forget: 'FORGET', feedback: 'RATING' }
        // a recall is logged with the PROMPT as its query — show both, so it is
        // never mistaken for a stored memory
        const detail = o.op === 'recall' ? (o.query ? `“${o.query}” → ${o.label}` : o.label) : o.label
        return (
          <div key={i} className={'obs-op ' + o.op + (o.ts === flashTs ? ' new' : '')} onClick={() => {
            if (!o.id) { setMobileDrawer(false); return }
            setSelected(o.id)
            setMobileDrawer(false)
            // the row may appear a beat before the graph refetch lands: if we do
            // not hold this atom yet, pull the graph so the panel has its text
            if (!nodesRef.current.some((n) => n.id === o.id)) loadGraph()
          }}>
            <span className="op">{badge[o.op] ?? o.op.toUpperCase()}</span>
            <span className="txt" title={o.op === 'recall' && o.query ? `recalled for: ${o.query}` : o.label}>{detail}</span>
            <span className="tm">{fmtTime(o.ts)}</span>
          </div>
        )
      })}
    </>
  )

  const playBtn = (
    <button className="obs-play" onClick={togglePlay} title={playing ? 'Pause' : 'Play growth'}>
      {playing ? <Pause size={17} /> : <Play size={17} style={{ marginLeft: 2 }} />}
    </button>
  )

  const sidebarContent = (
    <>
        <div className="obs-legend">
          <h6>
            Memories
            <button className="mini" onClick={() => { setHiddenTypes(new Set()); setHiddenHubs(new Set()); setHiddenEdges(new Set()) }}>show all</button>
          </h6>
          {Object.entries(ATOM_LABELS).map(([k, label]) => (
            <div className={'row toggle' + (hiddenTypes.has(k) ? ' off' : '')} key={k} onClick={() => toggle(setHiddenTypes, k)}>
              <span className="obs-dot" style={{ background: ATOM_COLORS[k] }} />
              <span>{label}</span>
              <span className="cnt">{hiddenTypes.has(k) ? <Minus size={14} /> : <Check size={14} />}</span>
            </div>
          ))}
          <h6>Hubs</h6>
          {hubList.map((h) => (
            <div className={'row toggle' + (hiddenHubs.has(h.id) ? ' off' : '')} key={h.id} onClick={() => toggle(setHiddenHubs, h.id)}>
              <span className="obs-dot" style={{ background: HUB_COLOR }} />
              <span>{h.label}</span>
              <span className="cnt">{hiddenHubs.has(h.id) ? <Minus size={14} /> : h.count}</span>
            </div>
          ))}
          <h6>Connections</h6>
          {Object.entries(EDGE_LABELS).map(([k, label]) => (
            <div className={'row toggle' + (hiddenEdges.has(k) ? ' off' : '')} key={k} onClick={() => toggle(setHiddenEdges, k)}>
              <span className="obs-line" style={{ background: EDGE_COLORS[k] }} />
              <span>{label}</span>
              <span className="cnt">{hiddenEdges.has(k) ? <Minus size={14} /> : (edgeCounts[k] || 0)}</span>
            </div>
          ))}
          <div className={'row toggle' + (signals ? '' : ' off')} onClick={() => setSignals((v) => !v)} title="Animate signals along the connections">
            <span className="obs-line" style={{ background: '#a5b4fc' }} />
            <span>Signals</span>
            <span className="cnt">{signals ? <Check size={14} /> : <Minus size={14} />}</span>
          </div>
        </div>
    </>
  )

  return (
    <div ref={rootRef} className={'obs' + (isMobile ? ' obs-mobile' : '')}>
      {isMobile ? (
        <header ref={headerRef} className="obs-header obs-header-m">
          <div className="obs-mbar">
            <button className="obs-iconbtn" onClick={() => setMobileDrawer(true)} title="Side panel" aria-label="Open side panel"><Menu size={18} /></button>
            <span className="obs-mark">{BRAND_MARK}</span>
            <span className="obs-title">OBSERVATORY</span>
            <div style={{ flex: 1 }} />
            <button className={'obs-iconbtn' + (live ? ' on' : '')} onClick={() => setLive((v) => !v)} title="Live memory feed" aria-label="Live memory feed"><Radio size={16} /></button>
            <button className={'obs-iconbtn' + (searchOpen ? ' on' : '')} onClick={() => setSearchOpen((v) => !v)} title="Search" aria-label="Search"><Search size={17} /></button>
          </div>
          {searchOpen && (
            <div className="obs-search-wrap obs-search-wrap-m">
              <div className="obs-search">
                <Search size={15} />
                <input autoFocus value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Find a memory, a decision, a thing…" spellCheck={false} />
              </div>
              {matches.length > 0 && (
                <div className="obs-results obs-results-m">
                  {matches.map((m) => (
                    <div key={m.id} className="r" onClick={() => { setSelected(m.id); setSearch(''); setSearchOpen(false) }}>
                      <span className="obs-dot" style={{ background: m.kind === 'hub' ? '#4b5a77' : ATOM_COLORS[m.type] }} />
                      <span className="lbl">{m.label}</span>
                      <span className="ty">{m.kind === 'hub' ? 'hub' : m.type}</span>
                    </div>
                  ))}
                </div>
              )}
            </div>
          )}
          <div ref={tabsRef} className="obs-tabs obs-tabs-m">
            {TABS.map(([key, label]) => (
              <button key={key} className={view === key ? 'on' : ''} onClick={() => { setView(key); setMobileDrawer(false) }}>{label}</button>
            ))}
          </div>
        </header>
      ) : (
      <header className="obs-header">
        <div className="obs-brand">
          <span className="obs-mark">{BRAND_MARK}</span>
          <span className="obs-title">OBSERVATORY</span>
          <span className="obs-sub">the brain · {mode === 'real' ? 'live memory' : 'prototype'}</span>
        </div>

        <div className="obs-search-wrap">
          <div className="obs-search">
            <Search size={15} />
            <input value={search} onChange={(e) => setSearch(e.target.value)} placeholder="Find a memory, a decision, a thing…" spellCheck={false} />
          </div>
          {matches.length > 0 && (
            <div className="obs-results">
              {matches.map((m) => (
                <div key={m.id} className="r" onClick={() => { setSelected(m.id); setSearch('') }}>
                  <span className="obs-dot" style={{ background: m.kind === 'hub' ? '#4b5a77' : ATOM_COLORS[m.type] }} />
                  <span className="lbl">{m.label}</span>
                  <span className="ty">{m.kind === 'hub' ? 'hub' : m.type}</span>
                </div>
              ))}
            </div>
          )}
        </div>

        <div className="obs-tabs" style={{ marginLeft: 12 }}>
          {TABS.map(([key, label]) => (
            <button key={key} className={view === key ? 'on' : ''} onClick={() => setView(key)}>{label}</button>
          ))}
        </div>

        <div style={{ flex: 1 }} />

        <button className={'obs-btn' + (live ? ' primary' : '')} onClick={() => setLive((v) => !v)} title="Simulated live memory feed">
          <Radio size={14} /> {live ? 'Live' : 'Live off'}
        </button>
        <button className="obs-btn" onClick={replay}><RotateCcw size={14} /> Replay growth</button>
        <button className={'obs-iconbtn' + (leftOpen ? ' on' : '')} onClick={() => setLeftOpen((v) => !v)} title="Filters"><PanelLeft size={16} /></button>
        <span className="obs-pill"><b>{visibleCount}</b> / {nodes.length} nodes</span>
      </header>
      )}

      {view === 'brain' && (
        <BrainCanvas
          nodes={nodes} links={links} timeEnd={timeEnd} selected={selected}
          onSelect={setSelected} search={search} fitSignal={fitSignal} pulse={pulse} hidden={hidden} show={showIds} signals={signals}
          insetTop={isMobile ? (headerH || 96) + 6 : 0} insetBottom={isMobile ? 108 : 0}
          insetLeft={isMobile || !leftOpen ? 0 : 300}
          insetRight={isMobile ? 0 : sel ? 404 : 322}
        />
      )}

      {view === 'brain' && !isMobile && leftOpen && (
        <div className="obs-left">{sidebarContent}</div>
      )}

      {/* live activity moves to the right rail; the detail panel takes its place when open */}
      {view === 'brain' && !isMobile && !sel && (
        <aside className="obs-right">
          <div className="obs-feed">
            <h6>
              <span className="live-dot" style={{ opacity: live ? 1 : 0.3 }} /> Live memory activity
              <span className="muted" style={{ marginLeft: 'auto', fontWeight: 400 }}>{realOps ? 'live' : 'simulated'} · {ops.length}</span>
            </h6>
            {activityRows}
          </div>
        </aside>
      )}

      {view === 'brain' && isMobile && (
        <>
          {mobileDrawer && <div className="obs-scrim" onClick={() => setMobileDrawer(false)} />}
          <aside className={'obs-left obs-drawer' + (mobileDrawer ? ' open' : '')} aria-hidden={!mobileDrawer}>
            <div className="obs-drawer-head">
              <span className="obs-drawer-title">Mind</span>
              <button className="obs-iconbtn" onClick={() => setMobileDrawer(false)} title="Close panel" aria-label="Close panel"><X size={16} /></button>
            </div>
            {sidebarContent}
          </aside>
        </>
      )}

      {view === 'brain' && sel && (
        <aside ref={sheetRef} className={'obs-panel' + (isMobile ? ' obs-panel-m' : '')}>
          {isMobile && (
            <div
              className="obs-sheet-grab"
              onTouchStart={onSheetStart}
              onTouchMove={onSheetMove}
              onTouchEnd={onSheetEnd}
              aria-label="Drag down to close"
            >
              <span />
            </div>
          )}
          <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
            <span className="obs-badge">
              <span className="obs-dot" style={{ background: sel.kind === 'hub' ? '#48566e' : ATOM_COLORS[sel.type] }} />
              {sel.kind === 'hub' ? 'hub' : sel.type}
            </span>
            <div style={{ flex: 1 }} />
            <button className="obs-iconbtn" onClick={() => setSelected(null)} title="Close"><X size={16} /></button>
          </div>
          <h2>{sel.label}</h2>

          {sel.kind === 'atom' ? (
            <>
              <div className="obs-sec" style={{ marginTop: 14 }}>What was memorized</div>
              <div className="obs-mem">{sel.text || '—'}</div>
              {recallInfo && (
                <div className="obs-mem" style={{ marginTop: 10, borderColor: 'rgba(129,140,248,.35)', color: '#c7d2fe' }}>
                  recalled <b>{recallInfo.n}×</b> · last {fmt(recallInfo.last)}
                </div>
              )}
              <div className="obs-kv">
                <div><span>type</span><b style={{ color: ATOM_COLORS[sel.type] }}>{sel.type}</b></div>
                <div><span>memorized</span><b className="mono">{fmt(sel.created)}</b></div>
                {sel.source && <div><span>source</span><b className="mono">{sel.source}</b></div>}
                <div><span>weight</span><b className="mono">{(sel.weight ?? 0.5).toFixed(2)}</b></div>
                {sel.tags && sel.tags.length > 0 && (
                  <div><span>tags</span><b>{sel.tags.map((t) => <span className="obs-tag" key={t}>#{t}</span>)}</b></div>
                )}
              </div>
            </>
          ) : (
            sel.text && <div className="body">{sel.text}</div>
          )}

          <div className="obs-sec">{connections.length} connection(s)</div>
          {connections.map((c, i) => (
            <div className="obs-conn" key={i} onClick={() => setSelected(c.node.id)}>
              <span className="rel" style={{ color: relColor(c.type) }}>
                {c.dir === 'in' ? '← ' : ''}{EDGE_LABELS[c.type] ?? c.type}{c.dir === 'out' ? ' →' : ''}
              </span>
              <span className="lbl">{c.node.label}</span>
            </div>
          ))}
        </aside>
      )}

      {view === 'dreams' && (
        <DreamsView
          onOpen={(id) => {
            setSelected(id)
            setView('brain')
          }}
        />
      )}

      {(view === 'identity' || view === 'infra') && (
        <ReaderView
          group={view}
          onOpen={(id) => {
            setSelected(id)
            setView('brain')
          }}
        />
      )}

      {view === 'memory' && (
        <MemoryView
          onOpen={(id) => {
            setSelected(id)
            setView('brain')
          }}
        />
      )}

      {view === 'brain' && (isMobile || !live) && (
      <div className={'obs-slider' + (!live ? ' obs-slider-history' : '')}>
        {isMobile && (
          <div className={'obs-activity' + (feedOpen ? ' open' : '')}>
            <button className="obs-activity-head" onClick={() => setFeedOpen((v) => !v)} aria-expanded={feedOpen}>
              <span className="live-dot" style={{ opacity: live ? 1 : 0.3 }} />
              <span>Activity</span>
              <span className="muted">{realOps ? 'live' : 'simulated'} · {ops.length}</span>
              <span className="chev">{feedOpen ? <ChevronDown size={15} /> : <ChevronUp size={15} />}</span>
            </button>
            {feedOpen && <div className="obs-activity-body">{activityRows}</div>}
          </div>
        )}
        {/* the time machine is only meaningful off-live, so it only exists then */}
        {!live && (
          <>
            <div className="obs-hist">
              <span className="obs-hist-dot" /> viewing history · <b>{fmt(timeEnd)}</b>
              <span className="obs-hint">drag the line to watch the mind grow</span>
            </div>
            <div className="obs-slider-row">
              {playBtn}
              <input
                className="tslider" type="range" min={minT} max={maxT} value={timeEnd}
                onChange={(e) => { progress.current = (Number(e.target.value) - minT) / Math.max(1, maxT - minT); setTimeEnd(Number(e.target.value)); setPlaying(false) }}
                style={{ background: `linear-gradient(to right, #9aa0cf ${pct}%, rgba(255,255,255,0.08) ${pct}%)` }}
              />
              <span className="obs-now-inline mono">{fmt(timeEnd)}</span>
            </div>
            <div className="obs-ticks"><span>{fmt(minT)}</span><span>{fmt(maxT)}</span></div>
          </>
        )}
      </div>
      )}
    </div>
  )
}
