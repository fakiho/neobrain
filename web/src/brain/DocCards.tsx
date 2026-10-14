import { useEffect, useState } from 'react'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { Check, Pencil, Sparkles, X } from 'lucide-react'

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

// Only the persona files the reflect runner also edits. USER.md / AGENTS.md
// are ledgers the user maintains by hand.
const EDITABLE = new Set(['IDENTITY.md', 'SOUL.md'])

/** The raw markdown docs as readable cards (Read/expand, Open in Brain). */
export function DocCards({ group, onOpen }: { group: DocGroup; onOpen: (id: string) => void }) {
  const [items, setItems] = useState<DocItem[]>([])
  const [loading, setLoading] = useState(true)
  const [open, setOpen] = useState<Set<string>>(new Set())
  const [editing, setEditing] = useState<string | null>(null)
  const [draft, setDraft] = useState('')
  const [saving, setSaving] = useState(false)
  const [note, setNote] = useState<{ path: string; kind: 'ok' | 'err'; text: string } | null>(null)

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

  const beginEdit = (it: DocItem) => {
    setEditing(it.path)
    setDraft(it.content)
    setNote(null)
    setOpen((prev) => new Set(prev).add(it.path))
  }

  const save = async (it: DocItem) => {
    setSaving(true)
    setNote(null)
    try {
      const r = await fetch('/api/mind/persona', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ name: it.name, content: draft }),
      })
      const body = await r.json().catch(() => ({}))
      if (!r.ok) {
        setNote({ path: it.path, kind: 'err', text: body.detail || `Save failed (HTTP ${r.status}).` })
        return
      }
      setItems((prev) => prev.map((x) => (x.path === it.path ? { ...x, content: draft, updated: Date.now() } : x)))
      setEditing(null)
      setNote({ path: it.path, kind: 'ok', text: 'Saved — the mind will pick this up on the next ingest.' })
    } catch {
      setNote({ path: it.path, kind: 'err', text: 'Save failed — the daemon is unreachable.' })
    } finally {
      setSaving(false)
    }
  }

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
              {it.atom_id && editing !== it.path && (
                <button className="obs-btn" onClick={() => onOpen(it.atom_id!)}>
                  <Sparkles size={13} /> Open in Brain
                </button>
              )}
              {EDITABLE.has(it.name) && editing !== it.path && (
                <button className="obs-btn" onClick={() => beginEdit(it)}>
                  <Pencil size={13} /> Edit
                </button>
              )}
              {editing !== it.path && (
                <button className="obs-btn" onClick={() => toggle(it.path)}>
                  {expanded ? 'Collapse' : 'Read'}
                </button>
              )}
            </div>
            {editing === it.path ? (
              <div className="doc-edit">
                <textarea
                  className="doc-editor"
                  value={draft}
                  onChange={(e) => setDraft(e.target.value)}
                  spellCheck={false}
                  aria-label={`Edit ${it.name}`}
                />
                <div className="doc-edit-bar">
                  <span className="muted">Markdown. Parenthesised values stay unfilled placeholders.</span>
                  <button className="obs-btn" onClick={() => setEditing(null)} disabled={saving}>
                    <X size={13} /> Cancel
                  </button>
                  <button className="obs-btn primary" onClick={() => save(it)} disabled={saving || draft === it.content}>
                    <Check size={13} /> {saving ? 'Saving…' : 'Save'}
                  </button>
                </div>
              </div>
            ) : (
              <div className={'doc-body' + (expanded ? '' : ' collapsed')}>
                <div className="md">
                  <ReactMarkdown remarkPlugins={[remarkGfm]}>{body}</ReactMarkdown>
                </div>
              </div>
            )}
            {note?.path === it.path && (
              <div className={'doc-note ' + note.kind} role={note.kind === 'err' ? 'alert' : 'status'}>{note.text}</div>
            )}
            {editing !== it.path && !expanded && <div className="doc-fade" />}
          </article>
        )
      })}
    </div>
  )
}
