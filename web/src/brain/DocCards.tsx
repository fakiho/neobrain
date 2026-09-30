import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Sparkles } from 'lucide-react'

export type DocGroup = 'memory' | 'identity' | 'infra'

export interface DocItem {
  name: string
  path: string
  updated: number
  content: string
  atom_id: string | null
}

// preferred display order for a group (unlisted names sort last)
const ORDER: Partial<Record<DocGroup, string[]>> = {
  identity: ['IDENTITY.md', 'SOUL.md', 'USER.md', 'AGENTS.md'],
}

/** The raw markdown docs as readable cards (Read/expand, Open in Brain). */
export function DocCards({ group, onOpen }: { group: DocGroup; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<DocItem[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState<Set<string>>(new Set())

  useEffect(() => {
    let alive = true
    setLoading(true)
    setOpen(new Set())
    fetch(`/api/mind/reader?group=${group}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
      .then((d) => {
        if (!alive) return
        let its: DocItem[] = d.items ?? []
        const order = ORDER[group]
        if (order) {
          its = [...its].sort((a, b) => {
            const ia = order.indexOf(a.name), ib = order.indexOf(b.name)
            return (ia < 0 ? 99 : ia) - (ib < 0 ? 99 : ib)
          })
        }
        setItems(its)
        setLoading(false)
      })
      .catch(() => alive && setLoading(false))
    return () => { alive = false }
  }, [group])

  const toggle = (p: string) =>
    setOpen((prev) => { const n = new Set(prev); n.has(p) ? n.delete(p) : n.add(p); return n })

  if (loading) return <div className="muted" style={{ padding: 20 }}>loading…</div>

  return (
    <div className="nights">
      {items.map((it) => {
        const expanded = open.has(it.path)
        const body = it.content.replace(/^#\s.*$/m, '').trim()
        return (
          <article className="night" key={it.path}>
            <div className="night-head">
              <span className="night-date mono">{it.name}</span>
              <span className="muted mono" style={{ fontSize: 10.5 }}>
                {new Date(it.updated).toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: '2-digit' })}
              </span>
              <div style={{ flex: 1 }} />
              {it.atom_id && (
                <button className="obs-btn" onClick={() => onOpen(it.atom_id!)}>
                  <Sparkles size={13} /> Open in Brain
                </button>
              )}
              <button className="obs-btn" onClick={() => toggle(it.path)}>
                {expanded ? 'Collapse' : 'Read'}
              </button>
            </div>
            <div className={'doc-body' + (expanded ? '' : ' collapsed')}>
              <div className="md">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
              </div>
            </div>
            {!expanded && <div className="doc-fade" />}
          </article>
        )
      })}
    </div>
  )
}
