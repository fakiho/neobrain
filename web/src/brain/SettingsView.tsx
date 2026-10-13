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
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [loadingModels, setLoadingModels] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [probe, setProbe] = useState<Record<string, any>>({})
  const [restartAsk, setRestartAsk] = useState<string[] | null>(null)
  const baseUrlRef = useRef<HTMLInputElement | null>(null)

  const load = useCallback((keepDraft = false) => {
    fetch('/api/settings')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: Snap) => { setSnap(d); if (!keepDraft) setDraft({}) })
      .catch(() => setMsg({ kind: 'err', text: 'Could not load settings — is the daemon up?' }))
  }, [])

  useEffect(() => { load() }, [load])

  // Success notes dismiss themselves; errors stay until dismissed (they carry
  // the why, and auto-hiding a failure is how it gets missed).
  useEffect(() => {
    if (msg?.kind !== 'ok') return
    const t = window.setTimeout(() => setMsg(null), 6000)
    return () => window.clearTimeout(t)
  }, [msg])

  // ⌘/Ctrl+S saves from anywhere on the page — no scrolling to a button.
  const saveRef = useRef<() => void>(() => {})
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key === 's') {
        e.preventDefault()
        saveRef.current()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const fields = useMemo(() => {
    const out: Record<string, Field> = {}
    for (const s of snap?.sections ?? []) for (const f of s.fields) out[f.key] = f
    return out
  }, [snap])

  const dirty = useMemo(() => Object.keys(draft), [draft])

  const set = (key: string, value: any) => setDraft((d) => ({ ...d, [key]: value }))
  const clear = (key: string) =>
    setDraft((d) => { const next = { ...d }; delete next[key]; return next })
  const current = (f: Field) => (f.key in draft ? draft[f.key] : f.value)

  const save = async () => {
    if (!dirty.length) return
    setBusy(true)
    setMsg(null)
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ values: draft }),
      })
      const body = await r.json()
      if (!r.ok) {
        setMsg({ kind: 'err', text: body.detail || `save failed (${r.status})` })
        return
      }
      setSnap(body)
      const needRestart = dirty
        .filter((k) => fields[k]?.restart)
        .map((k) => fields[k]?.label ?? k)
      setDraft({})
      setMsg({
        kind: 'ok',
        text: `Saved${body.applied?.length ? ` — ${body.applied.length} change(s)` : ''}` +
          `${body.reverted?.length ? `, ${body.reverted.length} reverted` : ''}.`,
      })
      if (needRestart.length) setRestartAsk(needRestart)
    } catch {
      setMsg({ kind: 'err', text: 'save failed — daemon unreachable' })
    } finally {
      setBusy(false)
    }
  }

  saveRef.current = () => { void save() }

  const restart = async () => {
    setBusy(true)
    setMsg(null)
    let accepted = false
    try {
      const r = await fetch('/api/settings/restart', { method: 'POST' })
      const b = await r.json().catch(() => ({}))
      if (r.ok && b.ok) accepted = true
      else setMsg({ kind: 'err', text: b.error || b.detail || `restart failed (${r.status})` })
    } catch {
      // The request dying mid-flight is expected: the restart kills this very process.
      accepted = true
    }
    setBusy(false)
    if (!accepted) return
    // Wait for the daemon to come back, then reload the snapshot so the page
    // reflects the restarted process (not the stale pre-restart values).
    setMsg({ kind: 'ok', text: 'Restarting the daemon — waiting for it to come back…' })
    const deadline = Date.now() + 45000
    const tick = () => {
      fetch('/api/health')
        .then((h) => {
          if (!h.ok) throw new Error('not ready')
          load(true)
          setMsg({ kind: 'ok', text: 'Daemon is back — settings reloaded.' })
        })
        .catch(() => {
          if (Date.now() < deadline) window.setTimeout(tick, 1500)
          else setMsg({ kind: 'err', text: 'restart was sent, but the daemon did not come back within 45s' })
        })
    }
    window.setTimeout(tick, 2500)
  }

  const loadModels = async () => {
    const baseUrl = String(current(fields.llm_base_url) ?? '')
    const apiKey = typeof draft.llm_api_key === 'string' ? draft.llm_api_key : ''
    setLoadingModels(true)
    setMsg(null)
    try {
      const r = await fetch('/api/settings/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      })
      const b = await r.json()
      if (!r.ok) { setMsg({ kind: 'err', text: b.detail || 'could not list models' }); return }
      setModels(b.models || [])
      setMsg({ kind: 'ok', text: `Found ${b.count} model(s) at ${b.base_url}.` })
    } catch {
      setMsg({ kind: 'err', text: 'could not reach the provider' })
    } finally {
      setLoadingModels(false)
    }
  }

  const testModel = async (model: string) => {
    const baseUrl = String(current(fields.llm_base_url) ?? '')
    const apiKey = typeof draft.llm_api_key === 'string' ? draft.llm_api_key : ''
    setTesting(model)
    setMsg(null)
    try {
      const r = await fetch('/api/settings/test', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null, model }),
      })
      const b = await r.json().catch(() => ({}))
      // A non-2xx answer carries {detail}, not {error} — without normalising it
      // the inline badge rendered an error icon with no text (invisible failure).
      const out = r.ok ? b : { ok: false, error: b.detail || `HTTP ${r.status}` }
      setProbe((p) => ({ ...p, [model]: out }))
      setMsg(out.ok
        ? { kind: 'ok', text: `Test ok — ${model} answered in ${out.latency_ms ?? '?'}ms.` }
        : { kind: 'err', text: `Test failed (${model}): ${out.error || 'unknown error'}` })
    } catch {
      setProbe((p) => ({ ...p, [model]: { ok: false, error: 'daemon unreachable' } }))
      setMsg({ kind: 'err', text: `Test failed (${model}): daemon unreachable` })
    } finally {
      setTesting(null)
    }
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
      <div className={'set-field' + (f.env_only ? ' env-only' : '')} key={f.key}>
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
            <div className="set-modelrow">
              <button className="set-mini" onClick={loadModels} disabled={loadingModels}>
                {loadingModels ? <Loader2 size={12} className="spin" /> : <RefreshCw size={12} />} Load models
              </button>
              <button className="set-mini" onClick={() => testModel(String(v))} disabled={!v || testing === v}>
                {testing === v ? <Loader2 size={12} className="spin" /> : <PlugZap size={12} />} Test
              </button>
              {probe[String(v)] && (
                <span className={'set-probe ' + (probe[String(v)].ok ? 'ok' : 'err')}>
                  {probe[String(v)].ok
                    ? <><Check size={12} /> {probe[String(v)].latency_ms}ms · {probe[String(v)].sample || 'ok'}</>
                    : <><AlertTriangle size={12} /> {probe[String(v)].error || 'failed'}</>}
                </span>
              )}
            </div>
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
            restart, and saving them will say so. Save is always in the bottom bar (or ⌘/Ctrl+S).
          </p>
        </div>
      </header>

      {!snap && !msg && <div className="muted" style={{ padding: 24 }}>loading…</div>}

      {snap && (
        <div className="set-grid">
          {cols.map((c, i) => (
            <div className="set-col" key={i}>
              {c.map((s) => (
                <section className="set-card" key={s.id} id={`set-${s.id}`}>
                  <h5>{s.label}</h5>
                  <p className="set-desc">{s.description}</p>
                  <div className="set-fields">{s.fields.map(renderField)}</div>
                </section>
              ))}
            </div>
          ))}
        </div>
      )}

      {/* Sticky action bar: Save/Restart and every result message live here, so
          feedback and the save control are visible from anywhere on the page. */}
      <div className="set-bar">
        {msg ? (
          <div className={'set-bar-msg ' + msg.kind} role="status">
            {msg.kind === 'ok' ? <Check size={13} /> : <AlertTriangle size={13} />}
            <span>{msg.text}</span>
            <button className="set-bar-x" onClick={() => setMsg(null)} title="Dismiss" aria-label="Dismiss message">
              <X size={12} />
            </button>
          </div>
        ) : (
          <span className={'set-bar-hint' + (dirty.length ? ' dirty' : '')}>
            {dirty.length ? `${dirty.length} unsaved change${dirty.length > 1 ? 's' : ''}` : 'all changes saved'}
          </span>
        )}
        <div style={{ flex: 1 }} />
        <button className="set-mini" onClick={restart} disabled={busy} title="Restart the daemon (e.g. after editing .env)">
          <Power size={12} /> Restart
        </button>
        <button className="set-save" onClick={save} disabled={busy || !dirty.length}>
          {busy ? <Loader2 size={14} className="spin" /> : <Save size={14} />} Save
        </button>
      </div>

      {restartAsk && (
        <div className="set-modal" role="dialog" aria-modal="true" onClick={() => setRestartAsk(null)}>
          <div className="set-modal-box" onClick={(e) => e.stopPropagation()}>
            <div className="set-modal-head">
              <Power size={15} />
              <h3>Restart required</h3>
            </div>
            <p>
              Saved <b>{restartAsk.join(', ')}</b>. {restartAsk.length > 1 ? 'These take' : 'It takes'} effect
              only after the daemon restarts — until then the running process keeps the old value.
            </p>
            <div className="set-modal-actions">
              <button className="set-mini" onClick={() => setRestartAsk(null)}>Later</button>
              <button className="set-save" onClick={() => { setRestartAsk(null); restart() }}>
                <Power size={14} /> Restart now
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
