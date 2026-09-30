import { useEffect, useRef } from 'react'
import type { PointerEvent as ReactPointerEvent, WheelEvent as ReactWheelEvent } from 'react'
import type { BrainLink, BrainNode } from './types'
import { ATOM_COLORS, EDGE_COLORS } from './theme'

// ---------------------------------------------------------------------------
// Neuron canvas — the Observatory's Brain view.
//
// Foundation: a deterministic "brain of neurons".
//   • each hub is a cell: an ellipse (wider than tall) sized to its contents
//   • memories sit on a golden-angle disc inside their cell, one spoke each
//   • cells are packed with a base gap, plus a wide moat around the central cell
//   • aggregated neuron↔neuron connections are arcs on a lower depth plane,
//     drawn behind opaque membranes so they pass under them and re-emerge
// It is static (no drifting), filter/time aware, and nodes/cells are draggable.
// ---------------------------------------------------------------------------

interface Props {
  nodes: BrainNode[]
  links: BrainLink[]
  timeEnd: number
  selected: string | null
  onSelect: (id: string | null) => void
  onHover?: (id: string | null) => void
  search: string
  fitSignal?: number
  pulse?: { ids: string[]; at: number } | null
  hidden?: { types: Set<string>; hubs: Set<string>; edges?: Set<string> }
  show?: Set<string>
  insetTop?: number
  insetBottom?: number
  insetLeft?: number
  insetRight?: number
  signals?: boolean
}

// tuned layout constants (mirroring the approved prototype)
const NODE_SCALE = 1.5
const ATOM_SPACING = 0.9
const HUB_GAP = 60
const CENTER_MOAT = 0.34
const PADX = 22
const PADY = 15
const WIDE = 1.10
const GOLDEN = Math.PI * (3 - Math.sqrt(5))
const HUB_FILL = '#0f1117'
const HUB_STROKE = '#5a6480'
// event signals pick a colour at random ("auto"), which tints both the node
// pulse and the hub flash so one impulse is traceable end-to-end
const SIG_PALETTE = ['#a5b4fc', '#f0c27a', '#5eead4', '#f2a8cd']
const EVENT_DELAY_MS = 2000            // real events: impulse departs 2 s later
const hexA = (hex: string, a: number) => {
  const n = parseInt(hex.slice(1), 16)
  return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${Math.max(0, Math.min(1, a)).toFixed(3)})`
}

interface SimNode extends BrainNode {
  x: number
  y: number
  r: number
  rx: number
  ry: number
  _atoms: SimNode[]
  _n: number
  _idx: number
  _lx: number
  _ly: number
  _rot: number
  _phase: number
}
interface SimLink {
  s: SimNode
  e: SimNode
  type: string
}
interface Cable {
  a: string
  b: string
  A: SimNode
  B: SimNode
  count: number
  dom: string
  types: Record<string, number>
}
// a queued event: resolved against the CURRENT layout when it fires, because a
// freshly stored atom only reaches the canvas after the graph refetch lands
interface Pending { targetId: string; at: number; color: string }
interface Impulse { srcHub: SimNode; toHub: SimNode; node: SimNode | null; cable: Cable | null; rev: boolean; visCount: number; nx: number; ny: number; t: number; sp: number; color: string; intra: boolean }
interface Arrival { cellId: string; nodeId: string | null; color: string; t0: number }
interface Layout {
  nodes: SimNode[]
  hubs: SimNode[]
  atoms: SimNode[]
  links: SimLink[]
  cables: Cable[]
  byId: Map<string, SimNode>
}

function mulberry32(a: number) {
  return function () {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function buildLayout(nodesIn: BrainNode[], linksIn: BrainLink[], W: number, H: number): Layout {
  const rnd = mulberry32(1337)
  const nodes: SimNode[] = nodesIn.map((n) => ({
    ...n, x: 0, y: 0, r: 0, rx: 0, ry: 0, _atoms: [], _n: 0, _idx: 0, _lx: 0, _ly: 0,
    _rot: 0, _phase: 0,
  }))
  const byId = new Map(nodes.map((n) => [n.id, n] as const))
  let hubs = nodes.filter((n) => n.kind === 'hub')
  const atoms = nodes.filter((n) => n.kind !== 'hub')
  hubs.forEach((h, i) => { h._rot = i * 1.7; h._phase = i * 0.9; h._atoms = [] })

  // assign memories to cells (atoms whose hub is unknown join a synthetic cell)
  const orphaned: SimNode[] = []
  for (const a of atoms) {
    const h = a.hub ? byId.get(a.hub) : undefined
    if (h && h.kind === 'hub') { a._idx = h._atoms.length; h._atoms.push(a) }
    else orphaned.push(a)
  }
  if (orphaned.length) {
    let hub = hubs.find((h) => h.id === '__none__')
    if (!hub) {
      hub = { id: '__none__', label: 'unsorted', kind: 'hub', type: 'hub', created: 0, x: 0, y: 0, r: 0, rx: 0, ry: 0, _atoms: [], _n: 0, _idx: 0, _lx: 0, _ly: 0, _rot: 0, _phase: 0 } as SimNode
      nodes.push(hub); byId.set('__none__', hub); hubs.push(hub)
    }
    hub._atoms = orphaned
    orphaned.forEach((a, i) => { a.hub = '__none__'; a._idx = i })
  }
  hubs.forEach((h) => { h._n = Math.max(1, h._atoms.length) })

  // size each cell to its contents, then pack the cells
  const cx = W / 2, cy = H / 2
  for (const h of hubs) {
    h.r = (9 + Math.sqrt(h._n) * 2.2) * Math.min(NODE_SCALE, 1.3)
    const inner = h.r + 22
    const gap = 10 + ATOM_SPACING * 16
    const outer = inner + gap * Math.sqrt(Math.max(0, h._n - 1))
    let maxX = inner, maxY = inner
    h._atoms.forEach((a) => {
      a.r = (2.4 + (a.weight ?? 0.5) * 2.6) * NODE_SCALE
      const t = (a._idx + 0.5) / Math.max(1, h._n)
      const rr = Math.sqrt(inner * inner + t * (outer * outer - inner * inner))
      const ang = a._idx * GOLDEN + h._rot
      const jr = rnd() * (outer - inner) * 0.05
      const ja = (rnd() - 0.5) * 0.07
      a._lx = Math.cos(ang + ja) * (rr + jr)
      a._ly = Math.sin(ang + ja) * (rr + jr)
      maxX = Math.max(maxX, Math.abs(a._lx) + a.r)
      maxY = Math.max(maxY, Math.abs(a._ly) + a.r)
    })
    h.rx = Math.max(inner + 10, maxX * WIDE + PADX)
    h.ry = Math.max(inner + 8, maxY + PADY)
  }

  const ordered = [...hubs].sort((a, b) => b.rx - a.rx)
  const center = ordered[0]
  const placed: SimNode[] = []
  for (const h of ordered) {
    if (!placed.length) { h.x = 0; h.y = 0; placed.push(h); continue }
    let done = false
    for (let ring = 8; ring < 6000 && !done; ring += 8) {
      const steps = Math.max(10, Math.floor((2 * Math.PI * ring) / Math.max(10, ring * 0.35)))
      for (let s = 0; s < steps; s++) {
        const ang = (s / steps) * Math.PI * 2 + ring * 0.31
        const x = Math.cos(ang) * ring, y = Math.sin(ang) * ring
        let ok = true
        for (const p of placed) {
          let need = h.rx + p.rx + HUB_GAP
          if (h === center || p === center) need += CENTER_MOAT * center.rx
          if (Math.hypot(x - p.x, y - p.y) < need) { ok = false; break }
        }
        if (ok) { h.x = x; h.y = y; placed.push(h); done = true; break }
      }
    }
    if (!done) { const a2 = placed.length * 2.399; h.x = Math.cos(a2) * 1600; h.y = Math.sin(a2) * 1600; placed.push(h) }
  }
  // centre the packed map and drop memories into world space
  let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity
  for (const h of hubs) {
    mnx = Math.min(mnx, h.x - h.rx); mny = Math.min(mny, h.y - h.ry)
    mxx = Math.max(mxx, h.x + h.rx); mxy = Math.max(mxy, h.y + h.ry)
  }
  const ox = (mnx + mxx) / 2, oy = (mny + mxy) / 2
  for (const h of hubs) {
    h.x = h.x - ox; h.y = h.y - oy
    for (const a of h._atoms) { a.x = h.x + a._lx; a.y = h.y + a._ly }
  }

  const links: SimLink[] = []
  for (const l of linksIn) {
    const s = byId.get(l.source), e = byId.get(l.target)
    if (s && e) links.push({ s, e, type: l.type })
  }
  // aggregate cross-cell links into one cable per cell pair
  const pair = new Map<string, Cable>()
  const neuronOf = (n: SimNode) => (n.kind === 'hub' ? n.id : n.hub || '__none__')
  for (const l of links) {
    const A = neuronOf(l.s), B = neuronOf(l.e)
    if (A === B) continue
    const na = byId.get(A), nb = byId.get(B)
    if (!na || !nb) continue
    const k = A < B ? A + '|' + B : B + '|' + A
    let c = pair.get(k)
    if (!c) { c = { a: A, b: B, A: na, B: nb, count: 0, dom: 'about', types: {} }; pair.set(k, c) }
    c.count++; c.types[l.type] = (c.types[l.type] || 0) + 1
  }
  for (const c of pair.values()) c.dom = Object.entries(c.types).sort((p, q) => q[1] - p[1])[0][0]
  const cables = [...pair.values()].sort((x, y) => y.count - x.count)

  return { nodes, hubs, atoms, links, cables, byId }
}

const radiusOf = (n: SimNode) => (n.kind === 'hub' ? n.r : n.r)

export function BrainCanvas({ nodes, links, timeEnd, selected, onSelect, onHover, search, fitSignal, pulse, hidden, show, insetTop = 0, insetBottom = 0, insetLeft = 0, insetRight = 0, signals = true }: Props) {
  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)
  const layRef = useRef<Layout | null>(null)
  const sigRef = useRef('')
  const hubSigRef = useRef('')
  const tf = useRef({ k: 1, x: 0, y: 0 })
  const sizeRef = useRef({ w: 800, h: 600 })
  const ptrs = useRef<Map<number, { x: number; y: number }>>(new Map())
  const pinch = useRef<{ d: number } | null>(null)
  const drag = useRef<{ x: number; y: number; moved: boolean; node: SimNode | null; atoms: SimNode[] | null } | null>(null)
  const hoverId = useRef<string | null>(null)
  const hoverTip = useRef<{ id: string; x: number; y: number } | null>(null)

  // prop mirrors for the animation loop
  const timeRef = useRef(timeEnd); timeRef.current = timeEnd
  const selectedRef = useRef(selected); selectedRef.current = selected
  const searchRef = useRef(search); searchRef.current = search
  const hiddenRef = useRef(hidden ?? { types: new Set(), hubs: new Set(), edges: new Set() }); hiddenRef.current = hidden ?? { types: new Set(), hubs: new Set(), edges: new Set() }
  const showRef = useRef(show ?? new Set()); showRef.current = show ?? new Set()
  const insetRef = useRef({ top: insetTop, bottom: insetBottom, left: insetLeft, right: insetRight })
  insetRef.current = { top: insetTop, bottom: insetBottom, left: insetLeft, right: insetRight }
  const pendingRef = useRef<Pending[]>([])
  const impulsesRef = useRef<Impulse[]>([])
  const arrivalsRef = useRef<Arrival[]>([])
  const frameLastRef = useRef(0)
  const signalsOnRef = useRef(signals); signalsOnRef.current = signals
  const sigsRef = useRef<{ c: Cable; t: number; sp: number; rev: boolean }[]>([])
  const sigNextRef = useRef(0)
  const sigMaxRef = useRef(3)
  const sigLastRef = useRef(0)

  const visible = (n: SimNode) => n.created <= timeRef.current
  const isHidden = (n: SimNode) =>
    showRef.current.has(n.id) ? false
      : n.kind === 'hub' ? hiddenRef.current.hubs.has(n.id)
        : hiddenRef.current.types.has(n.type) || (!!n.hub && hiddenRef.current.hubs.has(n.hub))
  const shown = (n: SimNode) => visible(n) && !isHidden(n)

  // (re)build the layout whenever the set of nodes changes
  useEffect(() => {
    const sig = nodes.map((n) => n.id).join(',')
    if (sig === sigRef.current && layRef.current) return
    sigRef.current = sig
    const { w, h } = sizeRef.current
    layRef.current = buildLayout(nodes, links, w, h)
    // framing is defined by the set of cells; re-fit when it changes (notably
    // the first real-data swap) so the graph is centred in the visible area at once
    const hubSig = nodes.filter((n) => n.kind === 'hub').map((n) => n.id).sort().join(',')
    if (hubSig !== hubSigRef.current) {
      hubSigRef.current = hubSig
      requestAnimationFrame(() => fit())
    }
  }, [nodes, links])

  // size the canvas
  useEffect(() => {
    const el = wrapRef.current, cv = canvasRef.current
    if (!el || !cv) return
    const apply = () => {
      const r = el.getBoundingClientRect()
      const dpr = Math.min(window.devicePixelRatio || 1, 2)
      const prev = sizeRef.current
      sizeRef.current = { w: r.width, h: r.height }
      cv.width = Math.max(1, Math.floor(r.width * dpr))
      cv.height = Math.max(1, Math.floor(r.height * dpr))
      cv.style.width = r.width + 'px'
      cv.style.height = r.height + 'px'
      // keep the graph framed when the viewport changes
      if (Math.abs(r.width - prev.w) > 0.5 || Math.abs(r.height - prev.h) > 0.5) requestAnimationFrame(() => fit())
    }
    apply()
    const ro = new ResizeObserver(apply)
    ro.observe(el)
    return () => ro.disconnect()
  }, [])

  // fit
  const fit = () => {
    const lay = layRef.current
    if (!lay) return
    const { w, h } = sizeRef.current
    const { top, bottom, left, right } = insetRef.current
    let mnx = Infinity, mny = Infinity, mxx = -Infinity, mxy = -Infinity
    for (const hub of lay.hubs) {
      if (!visible(hub) || isHidden(hub)) continue
      mnx = Math.min(mnx, hub.x - hub.rx); mny = Math.min(mny, hub.y - hub.ry)
      mxx = Math.max(mxx, hub.x + hub.rx); mxy = Math.max(mxy, hub.y + hub.ry)
    }
    if (!isFinite(mnx)) return
    const availW = Math.max(1, w - left - right)
    const availH = Math.max(1, h - top - bottom)
    const pad = Math.min(120, Math.min(availW, availH) * 0.08)
    const k = Math.max(0.1, Math.min(1.8, Math.min((availW - pad * 2) / Math.max(1, mxx - mnx), (availH - pad * 2) / Math.max(1, mxy - mny))))
    tf.current.k = k
    tf.current.x = left + availW / 2 - ((mnx + mxx) / 2) * k
    tf.current.y = top + availH / 2 - ((mny + mxy) / 2) * k
  }
  useEffect(() => { fit() }, [fitSignal, insetTop, insetBottom, insetLeft, insetRight]) // eslint-disable-line react-hooks/exhaustive-deps

  // event signals: a fresh op queues an impulse that departs after a short delay,
  // travels a wire (when its cell has one), then soma → the target node. The
  // colour is auto-picked and tints both the node pulse and the hub flash.
  useEffect(() => {
    if (!pulse?.ids?.length) return
    const now = performance.now()
    for (const id of pulse.ids) {
      pendingRef.current.push({
        targetId: id, at: now + EVENT_DELAY_MS,
        color: SIG_PALETTE[Math.floor(Math.random() * SIG_PALETTE.length)],
      })
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pulse])

  // animation loop
  useEffect(() => {
    let raf = 0
    const loop = () => { draw(); raf = requestAnimationFrame(loop) }
    const start = () => { if (!document.hidden && !raf) raf = requestAnimationFrame(loop) }
    const stop = () => { if (raf) cancelAnimationFrame(raf); raf = 0 }
    start()
    const onVis = () => (document.hidden ? stop() : start())
    document.addEventListener('visibilitychange', onVis)
    return () => { stop(); document.removeEventListener('visibilitychange', onVis) }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  function cableLift(c: Cable, count = c.count) {
    const d = Math.hypot(c.B.x - c.A.x, c.B.y - c.A.y) || 1
    return 38 * Math.min(1, d / 900) + (16 + Math.min(72, count * 2.2))
  }
  function cableGeom(c: Cable, count = c.count) {
    const dx = c.B.x - c.A.x, dy = c.B.y - c.A.y, d = Math.hypot(dx, dy) || 1
    const ux = dx / d, uy = dy / d, mx = (c.A.x + c.B.x) / 2, my = (c.A.y + c.B.y) / 2
    const px = -uy, py = ux, side = mx * px + my * py >= 0 ? 1 : -1
    const lift = cableLift(c, count)
    return { sx: c.A.x, sy: c.A.y, ex: c.B.x, ey: c.B.y, cx: mx + px * lift * side, cy: my + py * lift * side }
  }
  // two-leg path: wire (cell → cell) then soma → target node; intra-cell = one leg
  function impulsePoint(im: Impulse, tt: number) {
    if (im.intra) { const c = im.toHub; return { x: c.x + (im.nx - c.x) * tt, y: c.y + (im.ny - c.y) * tt } }
    const cut = 0.72
    if (tt <= cut) {
      const g = cableGeom(im.cable!, im.visCount)
      const u = im.rev ? 1 - tt / cut : tt / cut, m = 1 - u
      return { x: m * m * g.sx + 2 * m * u * g.cx + u * u * g.ex, y: m * m * g.sy + 2 * m * u * g.cy + u * u * g.ey }
    }
    const c = im.toHub, u = (tt - cut) / (1 - cut)
    return { x: c.x + (im.nx - c.x) * u, y: c.y + (im.ny - c.y) * u }
  }

  function neighbourSet(id: string): Set<string> {
    const lay = layRef.current
    const set = new Set<string>([id])
    if (!lay) return set
    for (const l of lay.links) {
      if (l.s.id === id) set.add(l.e.id)
      if (l.e.id === id) set.add(l.s.id)
    }
    return set
  }

  function draw() {
    const cv = canvasRef.current, lay = layRef.current
    if (!cv || !lay) return
    const ctx = cv.getContext('2d')!
    const dpr = Math.min(window.devicePixelRatio || 1, 2)
    const { w, h } = sizeRef.current
    const now = performance.now()
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.clearRect(0, 0, w, h)

    const t = tf.current
    ctx.save(); ctx.translate(t.x, t.y); ctx.scale(t.k, t.k)

    const sel = selectedRef.current
    const focus = sel || hoverId.current
    const selSet = focus ? neighbourSet(focus) : null
    const q = searchRef.current.trim().toLowerCase()
    const isMatch = (n: SimNode) => q.length > 1 && (n.label.toLowerCase().includes(q) || (n.text ?? '').toLowerCase().includes(q))

    const hiddenEdges = hiddenRef.current.edges ?? new Set<string>()

    // z-layer 0 — wiring plane (arcs + cast shadow), under the cells
    const geoms = new Map<Cable, { g: ReturnType<typeof cableGeom>; color: string }>()
    for (const c of lay.cables) {
      if (!shown(c.A) || !shown(c.B)) continue
      const hi = !!focus && (c.a === focus || c.b === focus)
      if (focus && !hi) continue
      // respect the Connections toggles: only count link types that are visible
      let vis = 0, top = 'about', topN = -1
      for (const [ty, n] of Object.entries(c.types)) { if (hiddenEdges.has(ty)) continue; vis += n; if (n > topN) { topN = n; top = ty } }
      if (!vis) continue
      const g = cableGeom(c, vis)
      geoms.set(c, { g, color: EDGE_COLORS[top] ?? '#8ea0cc' })
      const alpha = hi ? 0.9 : 0.10 + Math.min(0.24, vis / 50)
      ctx.globalAlpha = alpha * 0.5
      ctx.strokeStyle = 'rgba(3,4,8,0.9)'; ctx.lineWidth = (hi ? 2 : 1.3) / t.k
      ctx.shadowColor = 'rgba(0,0,0,0.55)'; ctx.shadowBlur = 9; ctx.shadowOffsetY = 6
      ctx.beginPath(); ctx.moveTo(g.sx, g.sy); ctx.quadraticCurveTo(g.cx, g.cy, g.ex, g.ey); ctx.stroke()
      ctx.shadowBlur = 0; ctx.shadowOffsetY = 0
      ctx.globalAlpha = alpha
      ctx.strokeStyle = hi ? '#b9c6ee' : '#8ea0cc'; ctx.lineWidth = (hi ? 1.3 : 0.9) / t.k
      ctx.beginPath(); ctx.moveTo(g.sx, g.sy); ctx.quadraticCurveTo(g.cx, g.cy, g.ex, g.ey); ctx.stroke()
    }
    ctx.globalAlpha = 1

    // signals: random 0–7 impulses traversing the wires, lower plane (occluded by cells)
    const reduce = matchMedia('(prefers-reduced-motion: reduce)').matches
    if (signalsOnRef.current && !reduce && geoms.size) {
      const dt = Math.min(50, now - (sigLastRef.current || now)); sigLastRef.current = now
      if (now > sigNextRef.current) { sigMaxRef.current = Math.floor(Math.random() * 8); sigNextRef.current = now + 1400 + Math.random() * 2600 }
      sigsRef.current = sigsRef.current.filter((s) => s.t < 1 && geoms.has(s.c))
      const cand = [...geoms.keys()]
      if (sigsRef.current.length < sigMaxRef.current && cand.length) {
        sigsRef.current.push({ c: cand[Math.floor(Math.random() * cand.length)], t: 0, sp: 0.22 + Math.random() * 0.5, rev: Math.random() < 0.5 })
      }
      for (const s of sigsRef.current) {
        s.t += (s.sp * dt) / 1000
        const hit = geoms.get(s.c); if (!hit) continue
        const { g, color } = hit
        for (let k = 0; k < 4; k++) {
          const tt = Math.max(0, Math.min(1, s.t - k * 0.035))
          const tpos = s.rev ? 1 - tt : tt
          const m2 = 1 - tpos
          const x = m2 * m2 * g.sx + 2 * m2 * tpos * g.cx + tpos * tpos * g.ex
          const y = m2 * m2 * g.sy + 2 * m2 * tpos * g.cy + tpos * tpos * g.ey
          ctx.globalAlpha = k === 0 ? 0.95 : Math.max(0, 0.5 - k * 0.12)
          ctx.fillStyle = k === 0 ? '#eef2ff' : color
          if (k === 0) { ctx.shadowColor = color; ctx.shadowBlur = 10 }
          ctx.beginPath(); ctx.arc(x, y, (k === 0 ? 2.4 : 1.5) / t.k, 0, Math.PI * 2); ctx.fill()
          ctx.shadowBlur = 0
        }
      }
      ctx.globalAlpha = 1
      ;(window as unknown as { __signals: number }).__signals = sigsRef.current.length
    } else {
      ;(window as unknown as { __signals: number }).__signals = 0
    }

    // event impulses: a fresh op travels its wire, then soma → the target node
    if (!reduce) {
      const dtm = Math.min(50, now - (frameLastRef.current || now)); frameLastRef.current = now
      const pend = pendingRef.current
      for (let i = pend.length - 1; i >= 0; i--) {
        if (now < pend[i].at) continue
        const p = pend[i]
        const node = lay.byId.get(p.targetId)          // may arrive with a later refetch
        if (!node) { if (now - p.at > 9000) pend.splice(i, 1); continue }
        pend.splice(i, 1)
        const isHub = node.kind === 'hub'
        const cellId = isHub ? node.id : (node.hub || '__none__')
        const toHub = lay.byId.get(cellId)
        if (!toHub) continue
        // source: a connected neighbour cell, else the cell itself (intra-cell)
        const inc = lay.cables.filter((c) => c.a === cellId || c.b === cellId)
        let sourceId = cellId, cable: Cable | null = null, rev = false
        if (inc.length) {
          const c = inc[Math.floor(Math.random() * inc.length)]
          const otherId = c.a === cellId ? c.b : c.a
          if (lay.byId.get(otherId)) { sourceId = otherId; cable = c; rev = c.a === cellId }
        }
        const visCount = cable ? Object.entries(cable.types).reduce((s, [ty, n]) => (hiddenEdges.has(ty) ? s : s + n), 0) : 0
        impulsesRef.current.push({
          srcHub: lay.byId.get(sourceId) ?? toHub, toHub, node, cable, rev, visCount,
          nx: node.x, ny: node.y,
          t: 0, sp: 0.3 + Math.random() * 0.2, color: p.color, intra: sourceId === cellId,
        })
      }
      for (let i = impulsesRef.current.length - 1; i >= 0; i--) {
        const im = impulsesRef.current[i]
        im.t += (im.sp * dtm) / 1000
        if (im.t >= 1) {
          impulsesRef.current.splice(i, 1)
          arrivalsRef.current.push({ cellId: im.toHub.id, nodeId: im.node ? im.node.id : null, color: im.color, t0: now })
          continue
        }
        for (let j = 0; j <= 7; j++) {
          const pt = impulsePoint(im, Math.max(0, im.t - j * 0.018))
          const a = Math.max(0, 1 - j / 7)
          ctx.globalAlpha = j === 0 ? 1 : a * 0.55
          ctx.fillStyle = j === 0 ? '#ffffff' : im.color
          if (j === 0) { ctx.shadowColor = im.color; ctx.shadowBlur = 14 }
          ctx.beginPath(); ctx.arc(pt.x, pt.y, (j === 0 ? 3 : 2) / t.k, 0, Math.PI * 2); ctx.fill()
          ctx.shadowBlur = 0
        }
      }
      ctx.globalAlpha = 1
      ;(window as unknown as { __fx: unknown }).__fx = {
        pending: pendingRef.current.length, impulses: impulsesRef.current.length, arrivals: arrivalsRef.current.length,
      }
    }

    // z-layer 1 — cell membranes (opaque, floating above the wiring)
    for (const hub of lay.hubs) {
      if (!visible(hub) || isHidden(hub)) continue
      const ph = hub._phase
      ctx.beginPath()
      for (let i = 0; i <= 48; i++) {
        const a = (i / 48) * Math.PI * 2
        const nz = 1 + 0.05 * Math.sin(a * 3 + ph) + 0.03 * Math.sin(a * 5 + ph * 1.7)
        const x = hub.x + Math.cos(a) * hub.rx * nz, y = hub.y + Math.sin(a) * hub.ry * nz
        i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)
      }
      ctx.closePath()
      const grad = ctx.createLinearGradient(hub.x, hub.y - hub.ry, hub.x, hub.y + hub.ry)
      grad.addColorStop(0, 'rgba(17,21,30,0.86)'); grad.addColorStop(1, 'rgba(9,11,16,0.78)')
      ctx.save()
      ctx.shadowColor = 'rgba(0,0,0,0.6)'; ctx.shadowBlur = 22; ctx.shadowOffsetY = 8
      ctx.fillStyle = grad; ctx.fill()
      ctx.restore()
      const selH = sel === hub.id || hoverId.current === hub.id
      ctx.strokeStyle = selH ? 'rgba(165,180,252,0.5)' : 'rgba(120,134,178,0.16)'
      ctx.lineWidth = (selH ? 1.3 : 0.8) / t.k; ctx.stroke()
    }

    // membership spokes + typed links (detail appears when focused)
    for (const l of lay.links) {
      if (!shown(l.s) || !shown(l.e)) continue
      if (hiddenEdges.has(l.type)) continue
      const memb = l.type === 'about' && ((l.s.kind !== 'hub' && l.s.hub === (l.e.kind === 'hub' ? l.e.id : '')) || (l.e.kind !== 'hub' && l.e.hub === (l.s.kind === 'hub' ? l.s.id : '')))
      const typed = l.type !== 'about'
      const touches = !!focus && (l.s.id === focus || l.e.id === focus)
      if (!memb && !touches) continue
      let alpha = memb ? 0.15 : 0.3
      let color = memb ? '#3a4152' : EDGE_COLORS[l.type] ?? '#7ba38c'
      let width = memb ? 0.8 : 1.15
      if (touches) { alpha = 0.9; width = 1.7 }
      else if (typed) continue
      const dim = !!selSet && !(selSet.has(l.s.id) && selSet.has(l.e.id))
      if (dim && !touches) alpha = 0.05
      ctx.globalAlpha = alpha; ctx.strokeStyle = color; ctx.lineWidth = width / t.k
      ctx.beginPath(); ctx.moveTo(l.s.x, l.s.y); ctx.lineTo(l.e.x, l.e.y); ctx.stroke()
    }
    ctx.globalAlpha = 1

    // soma glow (under memories)
    for (const hub of lay.hubs) {
      if (!shown(hub)) continue
      const dim = !!selSet && !selSet.has(hub.id)
      ctx.globalAlpha = dim ? 0.16 : 1
      const g = ctx.createRadialGradient(hub.x, hub.y, hub.r * 0.6, hub.x, hub.y, hub.r * 2.6)
      g.addColorStop(0, 'rgba(160,172,214,0.20)'); g.addColorStop(1, 'rgba(130,145,200,0)')
      ctx.fillStyle = g; ctx.beginPath(); ctx.arc(hub.x, hub.y, hub.r * 2.6, 0, Math.PI * 2); ctx.fill()
    }
    ctx.globalAlpha = 1

    // arrivals: the HUB flashes and the NODE pulses (hub does both when targeted)
    for (let i = arrivalsRef.current.length - 1; i >= 0; i--) {
      const ar = arrivalsRef.current[i], age = now - ar.t0
      if (age > 2600) { arrivalsRef.current.splice(i, 1); continue }
      const cell = lay.byId.get(ar.cellId)
      if (!cell || !shown(cell)) continue
      const p = age / 2600, col = ar.color
      const fl = Math.max(0, 1 - age / 700)
      const g = ctx.createRadialGradient(cell.x, cell.y, 0, cell.x, cell.y, Math.max(8, cell.rx * 1.25))
      g.addColorStop(0, hexA(col, 0.42 * fl)); g.addColorStop(1, hexA(col, 0))
      ctx.fillStyle = g; ctx.beginPath(); ctx.ellipse(cell.x, cell.y, cell.rx * 1.25, cell.ry * 1.25, 0, 0, Math.PI * 2); ctx.fill()
      ctx.globalAlpha = fl * 0.4; ctx.fillStyle = '#fff'
      ctx.beginPath(); ctx.ellipse(cell.x, cell.y, cell.rx * 0.92, cell.ry * 0.92, 0, 0, Math.PI * 2); ctx.fill(); ctx.globalAlpha = 1
      const node = ar.nodeId ? lay.byId.get(ar.nodeId) : null
      const base = node ? Math.max(3, node.r) : 13
      const cx = node ? node.x : cell.x, cy = node ? node.y : cell.y
      const maxR = node ? base + 34 : Math.min(cell.rx, cell.ry) + 46
      ctx.beginPath()
      if (node) ctx.arc(cx, cy, base + (maxR - base) * p, 0, Math.PI * 2)
      else ctx.ellipse(cx, cy, cell.rx + p * 46, cell.ry + p * 46, 0, 0, Math.PI * 2)
      ctx.strokeStyle = col; ctx.globalAlpha = (1 - p) * 0.9; ctx.lineWidth = 2 / t.k; ctx.stroke()
      const p2 = Math.max(0, p - 0.22)
      if (p2 > 0) {
        ctx.beginPath()
        if (node) ctx.arc(cx, cy, base + (maxR - base) * p2, 0, Math.PI * 2)
        else ctx.ellipse(cx, cy, cell.rx + p2 * 46, cell.ry + p2 * 46, 0, 0, Math.PI * 2)
        ctx.strokeStyle = col; ctx.globalAlpha = (1 - p2) * 0.45; ctx.lineWidth = 1.4 / t.k; ctx.stroke()
      }
      ctx.globalAlpha = 1
    }

    // memories (synapses)
    for (const a of lay.atoms) {
      if (!shown(a)) continue
      const col = ATOM_COLORS[a.type] ?? '#8f93a3'
      const dim = !!selSet && !selSet.has(a.id)
      const isFocus = a.id === focus
      ctx.globalAlpha = dim ? 0.1 : 1
      if (isFocus || (a.weight ?? 0) >= 0.9) { ctx.shadowColor = col; ctx.shadowBlur = isFocus ? 12 : 6 }
      ctx.fillStyle = col; ctx.beginPath(); ctx.arc(a.x, a.y, isFocus ? a.r * 1.35 : a.r, 0, Math.PI * 2); ctx.fill()
      ctx.shadowBlur = 0
      ctx.strokeStyle = 'rgba(8,9,13,.6)'; ctx.lineWidth = 0.8 / t.k; ctx.stroke()
      if (isFocus) { ctx.beginPath(); ctx.arc(a.x, a.y, a.r * 1.35 + 5, 0, Math.PI * 2); ctx.strokeStyle = '#a5b4fc'; ctx.lineWidth = 1.6 / t.k; ctx.stroke() }
      if (isMatch(a)) { ctx.beginPath(); ctx.arc(a.x, a.y, a.r + 9, 0, Math.PI * 2); ctx.strokeStyle = '#facc15'; ctx.lineWidth = 1.5 / t.k; ctx.stroke() }
    }
    ctx.globalAlpha = 1

    // soma bodies + labels (on top)
    for (const hub of lay.hubs) {
      if (!shown(hub)) continue
      const dim = !!selSet && !selSet.has(hub.id)
      const isFocus = hub.id === focus
      ctx.globalAlpha = dim ? 0.16 : 1
      ctx.beginPath(); ctx.arc(hub.x, hub.y, hub.r, 0, Math.PI * 2); ctx.fillStyle = HUB_FILL; ctx.fill()
      ctx.strokeStyle = isFocus ? '#aab0d0' : HUB_STROKE; ctx.lineWidth = (isFocus ? 1.9 : 1.4) / t.k; ctx.stroke()
      ctx.beginPath(); ctx.arc(hub.x, hub.y, Math.max(1.8, hub.r * 0.28), 0, Math.PI * 2); ctx.fillStyle = '#828aa6'; ctx.fill()
      if (isMatch(hub)) { ctx.beginPath(); ctx.arc(hub.x, hub.y, hub.r + 9, 0, Math.PI * 2); ctx.strokeStyle = '#facc15'; ctx.lineWidth = 1.5 / t.k; ctx.stroke() }
      ctx.textAlign = 'center'; ctx.textBaseline = 'top'
      ctx.font = '600 10px Inter, system-ui, sans-serif'
      try { (ctx as unknown as { letterSpacing: string }).letterSpacing = '1.3px' } catch { /* ignore */ }
      ctx.fillStyle = '#ccd6ea'; ctx.fillText(hub.label.toUpperCase(), hub.x, hub.y + hub.r + 5)
      try { (ctx as unknown as { letterSpacing: string }).letterSpacing = '0px' } catch { /* ignore */ }
      ctx.fillStyle = 'rgba(150,162,192,.62)'; ctx.font = '500 9px Inter, system-ui, sans-serif'
      ctx.fillText(String(hub._atoms.length), hub.x, hub.y + hub.r + 18)
    }
    ctx.globalAlpha = 1
    ctx.restore()
  }

  // ---- interaction ----
  function toWorld(px: number, py: number) {
    const t = tf.current
    return { x: (px - t.x) / t.k, y: (py - t.y) / t.k }
  }
  function nodeAt(px: number, py: number): SimNode | null {
    const lay = layRef.current
    if (!lay) return null
    const slop = (matchMedia('(pointer:coarse)').matches ? 10 : 0) / tf.current.k
    const { x: gx, y: gy } = toWorld(px, py)
    let best: SimNode | null = null, bd = Infinity
    for (const n of lay.nodes) {
      if (!shown(n)) continue
      const d = Math.hypot(n.x - gx, n.y - gy)
      const rr = (n.kind === 'hub' ? Math.max(n.r, 16) : radiusOf(n) + 7) + slop
      if (d < rr && d < bd) { best = n; bd = d }
    }
    return best
  }
  function cableAt(px: number, py: number): Cable | null {
    const lay = layRef.current
    if (!lay) return null
    const { x: gx, y: gy } = toWorld(px, py)
    let best: Cable | null = null, bd = 9 / tf.current.k
    for (const c of lay.cables) {
      if (!shown(c.A) || !shown(c.B)) continue
      const g = cableGeom(c)
      let d = Infinity
      for (let i = 0; i <= 14; i++) {
        const tt = i / 14, mt = 1 - tt
        const x = mt * mt * g.sx + 2 * mt * tt * g.cx + tt * tt * g.ex
        const y = mt * mt * g.sy + 2 * mt * tt * g.cy + tt * tt * g.ey
        d = Math.min(d, Math.hypot(gx - x, gy - y))
      }
      if (d < bd) { bd = d; best = c }
    }
    return best
  }

  function onPointerDown(e: ReactPointerEvent) {
    const cv = canvasRef.current!
    cv.setPointerCapture(e.pointerId)
    ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (ptrs.current.size >= 2) {
      const a = [...ptrs.current.values()]
      pinch.current = { d: Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y) || 1 }
      drag.current = null
      return
    }
    const n = nodeAt(e.clientX, e.clientY)
    drag.current = { x: e.clientX, y: e.clientY, moved: false, node: n, atoms: n && n.kind === 'hub' ? n._atoms : null }
    if (n) hoverId.current = null
    cv.style.cursor = 'grabbing'
  }
  function onPointerMove(e: ReactPointerEvent) {
    if (ptrs.current.has(e.pointerId)) ptrs.current.set(e.pointerId, { x: e.clientX, y: e.clientY })
    if (pinch.current && ptrs.current.size >= 2) {
      const a = [...ptrs.current.values()]
      const dist = Math.hypot(a[0].x - a[1].x, a[0].y - a[1].y) || 1
      const k2 = Math.max(0.1, Math.min(6, tf.current.k * (dist / pinch.current.d)))
      pinch.current = { d: dist }
      tf.current.x = a[0].x - ((a[0].x - tf.current.x) / tf.current.k) * k2
      tf.current.y = a[0].y - ((a[0].y - tf.current.y) / tf.current.k) * k2
      tf.current.k = k2
      return
    }
    const d = drag.current
    if (d) {
      const dx = e.clientX - d.x, dy = e.clientY - d.y
      const thr = e.pointerType === 'touch' ? 10 : 2
      if (!d.moved && Math.abs(dx) + Math.abs(dy) > thr) d.moved = true
      if (d.moved) {
        if (d.node) {
          const wdx = dx / tf.current.k, wdy = dy / tf.current.k
          d.node.x += wdx; d.node.y += wdy
          if (d.atoms) for (const a of d.atoms) { a.x += wdx; a.y += wdy }
        } else { tf.current.x += dx; tf.current.y += dy }
      }
      d.x = e.clientX; d.y = e.clientY
      return
    }
    const n = nodeAt(e.clientX, e.clientY)
    const id = n?.id ?? null
    if (id !== hoverId.current) { hoverId.current = id; onHover?.(id) }
    hoverTip.current = n ? { id: n.id, x: e.clientX - (canvasRef.current?.getBoundingClientRect().left ?? 0), y: e.clientY - (canvasRef.current?.getBoundingClientRect().top ?? 0) } : null
    canvasRef.current!.style.cursor = n ? 'pointer' : 'grab'
  }
  function onPointerUp(e: ReactPointerEvent) {
    const d = drag.current
    ptrs.current.delete(e.pointerId)
    if (ptrs.current.size < 2) pinch.current = null
    if (d && !d.moved) {
      const n = nodeAt(e.clientX, e.clientY)
      onSelect(n ? n.id : null)
    }
    drag.current = null
    if (canvasRef.current) canvasRef.current.style.cursor = 'grab'
  }
  function onWheel(e: ReactWheelEvent) {
    const cv = canvasRef.current!
    const r = cv.getBoundingClientRect()
    const px = e.clientX - r.left, py = e.clientY - r.top
    const k2 = Math.max(0.1, Math.min(6, tf.current.k * Math.exp(-e.deltaY * 0.0014)))
    tf.current.x = px - ((px - tf.current.x) / tf.current.k) * k2
    tf.current.y = py - ((py - tf.current.y) / tf.current.k) * k2
    tf.current.k = k2
  }

  const hoverNode = hoverTip.current ? nodes.find((n) => n.id === hoverTip.current!.id) : null

  return (
    <div ref={wrapRef} onDoubleClick={fit} style={{ position: 'absolute', inset: 0 }}>
      <canvas
        ref={canvasRef}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={(e) => { ptrs.current.delete(e.pointerId); if (ptrs.current.size < 2) pinch.current = null; drag.current = null }}
        onWheel={onWheel}
        style={{ display: 'block', cursor: 'grab', touchAction: 'none' }}
      />
      {hoverNode && hoverTip.current && (
        <div
          style={{
            position: 'absolute',
            left: Math.min(hoverTip.current.x + 16, sizeRef.current.w - 320),
            top: Math.min(hoverTip.current.y + 16, sizeRef.current.h - 90),
            maxWidth: 320, pointerEvents: 'none', zIndex: 6,
            background: 'rgba(20,24,34,0.96)', border: '1px solid var(--border-strong)',
            borderRadius: 11, padding: '9px 12px', boxShadow: 'var(--shadow)', backdropFilter: 'blur(8px)',
          }}
        >
          <div style={{ display: 'flex', gap: 7, alignItems: 'center', marginBottom: 5 }}>
            <span style={{ width: 8, height: 8, borderRadius: 3, background: hoverNode.kind === 'hub' ? '#5b6b8a' : ATOM_COLORS[hoverNode.type] }} />
            <span className="muted" style={{ fontSize: 9.5, textTransform: 'uppercase', letterSpacing: 1.4 }}>
              {hoverNode.kind === 'hub' ? 'hub' : hoverNode.type}
            </span>
          </div>
          <div style={{ fontWeight: 600, fontSize: 13 }}>{hoverNode.label}</div>
          {hoverNode.text && <div className="dim" style={{ fontSize: 12, marginTop: 3, lineHeight: 1.5 }}>{hoverNode.text}</div>}
        </div>
      )}
    </div>
  )
}
