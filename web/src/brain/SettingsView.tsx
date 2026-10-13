import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Settings as SettingsIcon, Save, RotateCcw, RefreshCw, PlugZap, Check,
  AlertTriangle, Loader2, Power, X,
} from 'lucide-react'

// Settings tab: enable/disable the brain's rituals, and point its cognition at
// any OpenAI-compatible endpoint (OpenAI, or a custom base URL + token) with a
// key + model chosen from the provider's own /models list.
//
// Writes go to PUT /api/settings (DB-backed overrides over .env).

type Field = {
  key: string
  label: string
  kind: string
  help: string
  choices: string[]
  restart: boolean
  env_only: boolean
  minimum: number | null
  maximum: number | null
  suffix: string
  secret: boolean
  overridden: boolean
  from_env: boolean
  value: any
  default: any
  set: boolean | null
}

type Section = { id: string; label: string; description: string; fields: Field[] }
type Provider = { id: string; label: string; base_url: string }
type Snap = {
  sections: Section[]
  providers: Provider[]
  life_enabled: boolean
  restart_pending: string[]
}

type Note = { kind: 'ok' | 'err'; text: string; title?: string; sticky?: boolean }

// A result shown right next to the control that produced it. Scrolls itself
// into view if it lands off-screen, so it can never be missed.
function InlineNote({ note, onClose }: { note: Note; onClose: () => void }) {
  const ref = useRef<HTMLDivElement | null>(null)
  useEffect(() => {
    ref.current?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
  }, [note])
  return (
    <div ref={ref} className={'set-inline ' + note.kind} role={note.kind === 'err' ? 'alert' : 'status'}>
      {note.kind === 'ok' ? <Check size={14} /> : <AlertTriangle size={14} />}
      <div className="set-inline-body">
        {note.title && <b>{note.title}</b>}
        <span>{note.text}</span>
      </div>
      <button className="set-inline-x" onClick={onClose} title="Dismiss" aria-label="Dismiss">
        <X size={12} />
      </button>
    </div>
  )
}

const truthy = (v: any) => v === true || ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase())

// Which column each section sits in, left to right. Anything not listed drops
// into the last column, so a new section can't silently disappear.
const COLUMNS: string[][] = [
  ['features', 'notifications', 'sources', 'daemon'],
  ['schedule', 'cognition'],
  ['memory', 'embeddings'],
]

export function SettingsView() {
  const [snap, setSnap] = useState<Snap | null>(null)
  const [draft, setDraft] = useState<Record<string, any>>({})
  const [loadErr, setLoadErr] = useState<string | null>(null)
  // Feedback lives where the action happened: one note per section card
  // (save / restart results) and one per model row (load / test results).
  const [notes, setNotes] = useState<Record<string, Note>>({})
  const [savingSection, setSavingSection] = useState<string | null>(null)
  const [restarting, setRestarting] = useState(false)
  const [models, setModels] = useState<string[]>([])
  const [modelsNote, setModelsNote] = useState<Note | null>(null)
  const [loadingModels, setLoadingModels] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [probe, setProbe] = useState<Record<string, any>>({})
  const [restartAsk, setRestartAsk] = useState<{ labels: string[]; section: string } | null>(null)
  const baseUrlRef = useRef<HTMLInputElement | null>(null)

  const note = (section: string, n: Note | null) =>
    setNotes((all) => {
      const next = { ...all }
      if (n) next[section] = n
      else delete next[section]
      return next
    })

  const load = useCallback((keepDraft = false) => {
    setLoadErr(null)
    fetch('/api/settings')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: Snap) => { setSnap(d); if (!keepDraft) setDraft({}) })
      .catch(() => setLoadErr('Could not load settings — is the daemon running?'))
  }, [])

  useEffect(() => { load() }, [load])

  // Success notes fade after a few seconds; errors stay until the next action
  // in that section (they carry the reason, so they must not vanish unread).
  useEffect(() => {
    const oks = Object.entries(notes).filter(([, n]) => n.kind === 'ok' && !n.sticky)
    if (!oks.length) return
    const t = window.setTimeout(() => {
      setNotes((all) => {
        const next = { ...all }
        for (const [k, n] of oks) if (next[k] === n) delete next[k]
        return next
      })
    }, 5000)
    return () => window.clearTimeout(t)
  }, [notes])

  const fields = useMemo(() => {
    const out: Record<string, Field> = {}
    for (const s of snap?.sections ?? []) for (const f of s.fields) out[f.key] = f
    return out
  }, [snap])

  const dirty = useMemo(() => Object.keys(draft), [draft])

  // Don't let unsaved edits vanish on reload / tab close.
  useEffect(() => {
    if (!dirty.length) return
    const warn = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', warn)
    return () => window.removeEventListener('beforeunload', warn)
  }, [dirty.length])
  const dirtyIn = (s: Section) => s.fields.filter((f) => f.key in draft).map((f) => f.key)

  const set = (key: string, value: any) => setDraft((d) => ({ ...d, [key]: value }))
  const clear = (key: string) =>
    setDraft((d) => { const next = { ...d }; delete next[key]; return next })
  const current = (f: Field) => (f.key in draft ? draft[f.key] : f.value)

  // Save only one section's edits — the save control sits in that card, right
  // under the fields that were changed. Other cards keep their pending edits.
  const saveSection = async (s: Section) => {
    const keys = dirtyIn(s)
    if (!keys.length) return
    const values = Object.fromEntries(keys.map((k) => [k, draft[k]]))
    setSavingSection(s.id)
    note(s.id, null)
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values }),
      })
      const body = await r.json().catch(() => ({}))
      if (!r.ok) {
        note(s.id, { kind: 'err', text: body.detail || `Save failed (HTTP ${r.status}).` })
        return
      }
      setSnap(body)
      setDraft((d) => {
        const next = { ...d }
        for (const k of keys) delete next[k]
        return next
      })
      const needRestart = keys.filter((k) => fields[k]?.restart).map((k) => fields[k]?.label ?? k)
      note(s.id, {
        kind: 'ok',
        text: `Saved ${keys.length} change${keys.length > 1 ? 's' : ''}` +
          `${body.reverted?.length ? ` (${body.reverted.length} reverted to default)` : ''}.`,
      })
      if (needRestart.length) setRestartAsk({ labels: needRestart, section: s.id })
    } catch {
      note(s.id, { kind: 'err', text: 'Save failed — the daemon is unreachable.' })
    } finally {
      setSavingSection(null)
    }
  }

  const discardSection = (s: Section) => {
    const keys = dirtyIn(s)
    setDraft((d) => {
      const next = { ...d }
      for (const k of keys) delete next[k]
      return next
    })
    note(s.id, null)
  }

  const restart = async (section = 'daemon') => {
    setRestarting(true)
    note(section, null)
    let accepted = false
    try {
      const r = await fetch('/api/settings/restart', { method: 'POST' })
      const b = await r.json().catch(() => ({}))
      if (r.ok && b.ok) accepted = true
      else note(section, { kind: 'err', text: b.error || b.detail || `Restart failed (HTTP ${r.status}).` })
    } catch {
      // The request dying mid-flight is expected: the restart kills this very process.
      accepted = true
    }
    if (!accepted) { setRestarting(false); return }
    // Wait for the daemon to come back, then reload the snapshot so the page
    // reflects the restarted process (not the stale pre-restart values).
    note(section, { kind: 'ok', text: 'Restarting — waiting for the daemon to come back…', sticky: true })
    const deadline = Date.now() + 45000
    const tick = () => {
      fetch('/api/health')
        .then((h) => {
          if (!h.ok) throw new Error('not ready')
          load(true)
          setRestarting(false)
          note(section, { kind: 'ok', text: 'Daemon restarted — settings reloaded.' })
        })
        .catch(() => {
          if (Date.now() < deadline) { window.setTimeout(tick, 1500); return }
          setRestarting(false)
          note(section, { kind: 'err', text: 'Restart was sent, but the daemon did not come back within 45s.' })
        })
    }
    window.setTimeout(tick, 2500)
  }

  const loadModels = async () => {
    const baseUrl = String(current(fields.llm_base_url) ?? '')
    const apiKey = typeof draft.llm_api_key === 'string' ? draft.llm_api_key : ''
    setLoadingModels(true)
    setModelsNote(null)
    try {
      const r = await fetch('/api/settings/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      })
      const b = await r.json().catch(() => ({}))
      if (!r.ok) { setModelsNote({ kind: 'err', text: b.detail || `Could not list models (HTTP ${r.status}).` }); return }
      setModels(b.models || [])
      setModelsNote({ kind: 'ok', text: `${b.count} model${b.count === 1 ? '' : 's'} found — pick one from the field's suggestions.` })
    } catch {
      setModelsNote({ kind: 'err', text: 'Could not reach the daemon.' })
    } finally {
      setLoadingModels(false)
    }
  }

  const testModel = async (model: string) => {
    const baseUrl = String(current(fields.llm_base_url) ?? '')
    const apiKey = typeof draft.llm_api_key === 'string' ? draft.llm_api_key : ''
    setTesting(model)
    setProbe((p) => { const next = { ...p }; delete next[model]; return next })
    try {
      const r = await fetch('/api/settings/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null, model }),
      })
      const b = await r.json().catch(() => ({}))
      // A non-2xx answer carries {detail}, not {error}: normalise it so the
      // failure always has readable text.
      const out = r.ok ? b : { ok: false, error: b.detail || `HTTP ${r.status}` }
      setProbe((p) => ({ ...p, [model]: out }))
    } catch {
      setProbe((p) => ({ ...p, [model]: { ok: false, error: 'daemon unreachable' } }))
    } finally {
      setTesting(null)
    }
  }

  const jumpToFirstDirty = () => {
    const s = snap?.sections.find((x) => dirtyIn(x).length)
    if (s) document.getElementById(`set-${s.id}`)?.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const renderField = (f: Field) => {
    const v = current(f)
    const dirtyField = f.key in draft
    const input = () => {
      if (f.kind === 'bool') {
        return (
          <button
            type="button"
            className={'set-switch' + (truthy(v) ? ' on' : '')}
            onClick={() => set(f.key, truthy(v) ? '0' : '1')}
            aria-pressed={truthy(v)}
            disabled={f.env_only}
          >
            <span className="knob" />
          </button>
        )
      }
      if (f.kind === 'secret') {
        return (
          <input
            className="set-input mono"
            type="password"
            autoComplete="off"
            placeholder={f.set ? '•••••••• (set — leave blank to keep)' : 'not set'}
            value={v ?? ''}
            onChange={(e) => set(f.key, e.target.value)}
            disabled={f.env_only}
          />
        )
      }
      if (f.kind === 'choice') {
        return (
          <select className="set-input" value={String(v ?? '')} onChange={(e) => set(f.key, e.target.value)} disabled={f.env_only}>
            {f.choices.map((c) => <option key={c} value={c}>{c}</option>)}
          </select>
        )
      }
      if (f.kind === 'int' || f.kind === 'float') {
        return (
          <div className="set-num">
            <input
              className="set-input mono"
              type="number"
              step={f.kind === 'float' ? 'any' : 1}
              min={f.minimum ?? undefined}
              max={f.maximum ?? undefined}
              value={v ?? ''}
              onChange={(e) => set(f.key, e.target.value)}
              disabled={f.env_only}
            />
            {f.suffix && <span className="suf">{f.suffix}</span>}
          </div>
        )
      }
      const listId = ['llm_model_cheap', 'llm_model_strong'].includes(f.key) && models.length ? `models-${f.key}` : undefined
      return (
        <>
          <input
            className="set-input mono"
            list={listId}
            ref={f.key === 'llm_base_url' ? baseUrlRef : undefined}
            value={v ?? ''}
            onChange={(e) => set(f.key, e.target.value)}
            disabled={f.env_only}
            placeholder={f.key === 'llm_model_cheap' ? 'e.g. deepseek/deepseek-v4.1-flash' : undefined}
          />
          {listId && <datalist id={listId}>{models.map((m) => <option key={m} value={m} />)}</datalist>}
        </>
      )
    }
    return (
      <div className={'set-field' + (f.env_only ? ' env-only' : '') + (dirtyField ? ' dirty' : '')} key={f.key}>
        <div className="set-field-head">
          <label>{f.label}</label>
          {f.from_env && <span className="set-tag env" title="Also set in the environment">env</span>}
          {!f.env_only && (dirtyField || f.overridden) && (
            <button
              className="set-revert"
              title={dirtyField ? 'Discard this edit' : 'Revert to the .env default'}
              onClick={() => (dirtyField ? clear(f.key) : set(f.key, null))}
            >
              <RotateCcw size={12} />
            </button>
          )}
        </div>
        <div className="set-field-body">
          <div className="set-control">{input()}</div>
          {f.help && <div className="set-help">{f.help}</div>}
          {f.key === 'llm_base_url' && (
            <div className="set-presets">
              {snap?.providers.map((p) => (
                <button
                  key={p.id}
                  className={'set-chip' + (p.base_url && String(v) === p.base_url ? ' on' : '')}
                  onClick={() => {
                    if (p.base_url) set('llm_base_url', p.base_url)
                    else { baseUrlRef.current?.focus(); baseUrlRef.current?.select() }
                  }}
                  title={p.base_url || 'type any OpenAI-compatible base URL below'}
                >
                  {p.label}
                </button>
              ))}
            </div>
          )}
          {(f.key === 'llm_model_cheap' || f.key === 'llm_model_strong') && (
            <>
              <div className="set-modelrow">
                <button className="set-mini" onClick={loadModels} disabled={loadingModels}>
                  {loadingModels ? <Loader2 size={12} className="spin" /> : <RefreshCw size={12} />}
                  {loadingModels ? 'Loading…' : 'Load models'}
                </button>
                <button className="set-mini" onClick={() => testModel(String(v))} disabled={!v || testing === v}>
                  {testing === v ? <Loader2 size={12} className="spin" /> : <PlugZap size={12} />}
                  {testing === v ? 'Testing…' : 'Test'}
                </button>
              </div>
              {probe[String(v)] && (
                <InlineNote
                  note={probe[String(v)].ok
                    ? { kind: 'ok', text: `Works — answered in ${probe[String(v)].latency_ms}ms (“${probe[String(v)].sample || 'ok'}”).` }
                    : { kind: 'err', title: 'Test failed', text: probe[String(v)].error || 'unknown error' }}
                  onClose={() => setProbe((p) => { const next = { ...p }; delete next[String(v)]; return next })}
                />
              )}
              {f.key === 'llm_model_cheap' && modelsNote && (
                <InlineNote note={modelsNote} onClose={() => setModelsNote(null)} />
              )}
            </>
          )}
        </div>
      </div>
    )
  }

  const cols = useMemo(() => {
    const out: Section[][] = COLUMNS.map(() => [])
    const at = new Map<string, number>()
    COLUMNS.forEach((ids, i) => ids.forEach((id) => at.set(id, i)))
    for (const s of snap?.sections ?? []) out[at.get(s.id) ?? out.length - 1].push(s)
    return out.filter((c) => c.length)
  }, [snap])

  return (
    <div className="settings">
      <header className="set-head">
        <div>
          <h1><SettingsIcon size={18} /> Settings</h1>
          <p className="muted">
            Overrides the <span className="mono">.env</span> defaults. Every change applies live;
            the daemon's address fields (<b>Bind</b>, <b>API URL</b>) only take effect after a
            restart, and saving them will say so. Each card saves its own changes.
          </p>
        </div>
        {dirty.length > 0 && (
          <button className="set-pending" onClick={jumpToFirstDirty} title="Scroll to the first card with unsaved edits">
            {dirty.length} unsaved change{dirty.length > 1 ? 's' : ''} — show
          </button>
        )}
      </header>

      {loadErr && (
        <div className="set-inline err set-loaderr" role="alert">
          <AlertTriangle size={14} />
          <span>{loadErr}</span>
          <button className="set-mini" onClick={() => load()}><RefreshCw size={12} /> Retry</button>
        </div>
      )}
      {!snap && !loadErr && <div className="muted" style={{ padding: 24 }}>loading…</div>}

      {snap && (
        <div className="set-grid">
          {cols.map((c, i) => (
            <div className="set-col" key={i}>
              {c.map((s) => {
                const pending = dirtyIn(s)
                const saving = savingSection === s.id
                return (
                  <section className={'set-card' + (pending.length ? ' dirty' : '')} key={s.id} id={`set-${s.id}`}>
                    <h5>{s.label}</h5>
                    <p className="set-desc">{s.description}</p>
                    <div className="set-fields">{s.fields.map(renderField)}</div>

                    {s.id === 'daemon' && (
                      <div className="set-card-extra">
                        <button className="set-mini" onClick={() => restart(s.id)} disabled={restarting}
                          title="Restart the daemon (e.g. after editing .env)">
                          {restarting ? <Loader2 size={12} className="spin" /> : <Power size={12} />}
                          {restarting ? 'Restarting…' : 'Restart daemon'}
                        </button>
                      </div>
                    )}

                    {/* Save sits in the card, right under the edited fields, and
                        only appears when this card has something to save. */}
                    {pending.length > 0 && (
                      <div className="set-card-actions">
                        <span className="set-card-pending">
                          {pending.length} unsaved change{pending.length > 1 ? 's' : ''}
                        </span>
                        <button className="set-mini" onClick={() => discardSection(s)} disabled={saving}>
                          <X size={12} /> Discard
                        </button>
                        <button className="set-save" onClick={() => saveSection(s)} disabled={saving}>
                          {saving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
                          {saving ? 'Saving…' : 'Save'}
                        </button>
                      </div>
                    )}

                    {notes[s.id] && <InlineNote note={notes[s.id]} onClose={() => note(s.id, null)} />}
                  </section>
                )
              })}
            </div>
          ))}
        </div>
      )}

      {restartAsk && (
        <div className="set-modal" role="dialog" aria-modal="true" onClick={() => setRestartAsk(null)}>
          <div className="set-modal-box" onClick={(e) => e.stopPropagation()}>
            <div className="set-modal-head">
              <Power size={15} />
              <h3>Restart required</h3>
            </div>
            <p>
              Saved <b>{restartAsk.labels.join(', ')}</b>. {restartAsk.labels.length > 1 ? 'These take' : 'It takes'} effect
              only after the daemon restarts — until then the running process keeps the old value.
            </p>
            <div className="set-modal-actions">
              <button className="set-mini" onClick={() => setRestartAsk(null)}>Later</button>
              <button className="set-save" onClick={() => { const at = restartAsk.section; setRestartAsk(null); restart(at) }}>
                <Power size={14} /> Restart now
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
