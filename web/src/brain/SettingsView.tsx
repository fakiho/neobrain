import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Settings as SettingsIcon, Save, RotateCcw, RefreshCw, PlugZap, Check,
  AlertTriangle, Loader2, KeyRound, Power,
} from 'lucide-react'

// Settings tab: enable/disable the brain's rituals, and point its cognition at
// a provider (in-house LiteLLM, LLM Gateway, or any OpenAI-compatible endpoint)
// with a key + model chosen from the provider's own /models list.
//
// Writes go to PUT /api/settings (DB-backed overrides over .env). When the
// daemon has NEOBRAIN_SETTINGS_TOKEN set, every write — and the provider
// probes — must carry it as X-NeoBrain-Token; it is kept in localStorage.

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
  token_required: boolean
  life_enabled: boolean
  restart_pending: string[]
}

const TOKEN_KEY = 'neobrain-settings-token'
const truthy = (v: any) => v === true || ['1', 'true', 'yes', 'on'].includes(String(v).toLowerCase())

export function SettingsView() {
  const [snap, setSnap] = useState<Snap | null>(null)
  const [draft, setDraft] = useState<Record<string, any>>({})
  const [token, setToken] = useState<string>(() => localStorage.getItem(TOKEN_KEY) || '')
  const [busy, setBusy] = useState(false)
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null)
  const [models, setModels] = useState<string[]>([])
  const [loadingModels, setLoadingModels] = useState(false)
  const [testing, setTesting] = useState<string | null>(null)
  const [probe, setProbe] = useState<Record<string, any>>({})

  const authHeaders = (): Record<string, string> =>
    token ? { 'X-NeoBrain-Token': token } : {}

  const load = useCallback(() => {
    fetch('/api/settings')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(String(r.status)))))
      .then((d: Snap) => { setSnap(d); setDraft({}) })
      .catch(() => setMsg({ kind: 'err', text: 'Could not load settings — is the daemon up?' }))
  }, [])

  useEffect(() => { load() }, [load])
  useEffect(() => { localStorage.setItem(TOKEN_KEY, token) }, [token])

  const fields = useMemo(() => {
    const out: Record<string, Field> = {}
    for (const s of snap?.sections ?? []) for (const f of s.fields) out[f.key] = f
    return out
  }, [snap])

  const dirty = useMemo(() => Object.keys(draft), [draft])

  const set = (key: string, value: any) => setDraft((d) => ({ ...d, [key]: value }))
  const current = (f: Field) => (f.key in draft ? draft[f.key] : f.value)

  const save = async () => {
    if (!dirty.length) return
    setBusy(true)
    setMsg(null)
    try {
      const r = await fetch('/api/settings', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ values: draft }),
      })
      if (r.status === 401) {
        setMsg({ kind: 'err', text: 'Settings token required — enter it above and save again.' })
        return
      }
      const body = await r.json()
      if (!r.ok) {
        setMsg({ kind: 'err', text: body.detail || `save failed (${r.status})` })
        return
      }
      setSnap(body)
      // If the token itself was just set, carry it into the auth field so the
      // next write is already authenticated.
      if (typeof draft.settings_token === 'string' && draft.settings_token.trim()) {
        setToken(draft.settings_token.trim())
      }
      setDraft({})
      const pend = body.restart_pending?.length
      setMsg({
        kind: 'ok',
        text: `Saved${body.applied?.length ? ` — ${body.applied.length} change(s)` : ''}` +
          `${body.reverted?.length ? `, ${body.reverted.length} reverted` : ''}` +
          `${pend ? ` · ${pend} need a restart` : ''}.`,
      })
    } catch {
      setMsg({ kind: 'err', text: 'save failed — daemon unreachable' })
    } finally {
      setBusy(false)
    }
  }

  const restart = async () => {
    setBusy(true)
    try {
      const r = await fetch('/api/settings/restart', { method: 'POST', headers: authHeaders() })
      const b = await r.json()
      setMsg(r.ok && b.ok
        ? { kind: 'ok', text: 'Restarting the daemon — the page will reconnect shortly.' }
        : { kind: 'err', text: b.error || b.detail || 'restart failed' })
    } catch {
      setMsg({ kind: 'ok', text: 'Restarting — the connection dropped as expected.' })
    } finally {
      setBusy(false)
    }
  }

  const loadModels = async () => {
    const baseUrl = String(current(fields.llm_base_url) ?? '')
    const apiKey = typeof draft.llm_api_key === 'string' ? draft.llm_api_key : ''
    setLoadingModels(true)
    setMsg(null)
    try {
      const r = await fetch('/api/settings/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null }),
      })
      const b = await r.json()
      if (r.status === 401) { setMsg({ kind: 'err', text: 'Settings token required.' }); return }
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
        headers: { 'Content-Type': 'application/json', ...authHeaders() },
        body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || null, model }),
      })
      const b = await r.json()
      if (r.status === 401) { setMsg({ kind: 'err', text: 'Settings token required.' }); return }
      setProbe((p) => ({ ...p, [model]: b }))
    } catch {
      setProbe((p) => ({ ...p, [model]: { ok: false, error: 'unreachable' } }))
    } finally {
      setTesting(null)
    }
  }

  const renderField = (f: Field) => {
    const v = current(f)
    const overridden = f.overridden || f.key in draft
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
          {f.restart && <span className="set-tag restart" title="Takes effect after a daemon restart">restart</span>}
          {f.from_env && <span className="set-tag env" title="Also set in the environment">env</span>}
          {overridden && !f.env_only && (
            <button className="set-revert" title="Revert to the .env default" onClick={() => set(f.key, null)}>
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
                  className={'set-chip' + (String(v) === p.base_url ? ' on' : '')}
                  onClick={() => set('llm_base_url', p.base_url)}
                  title={p.base_url || 'enter a URL below'}
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
                    : <><AlertTriangle size={12} /> {probe[String(v)].error}</>}
                </span>
              )}
            </div>
          )}
        </div>
      </div>
    )
  }

  const pending = snap?.restart_pending ?? []
  const restartFields = pending.map((k) => fields[k]?.label ?? k)

  return (
    <div className="settings">
      <header className="set-head">
        <div>
          <h1><SettingsIcon size={18} /> Settings</h1>
          <p className="muted">
            Overrides the <span className="mono">.env</span> defaults. Changes apply live; keys marked
            <span className="set-tag restart">restart</span> need a daemon restart.
          </p>
        </div>
        <div className="set-head-r">
          <div className={'set-token' + (snap?.token_required ? ' on' : '')}>
            <KeyRound size={13} />
            <input
              type="password"
              placeholder={snap?.token_required ? 'settings token' : 'no token set'}
              value={token}
              onChange={(e) => setToken(e.target.value)}
              autoComplete="off"
              spellCheck={false}
            />
          </div>
          {dirty.length > 0 && <span className="set-dirty">{dirty.length} unsaved</span>}
          <button className="set-save" onClick={save} disabled={busy || !dirty.length}>
            {busy ? <Loader2 size={14} className="spin" /> : <Save size={14} />} Save
          </button>
        </div>
      </header>

      {!snap?.token_required && (
        <div className="set-note warn">
          <AlertTriangle size={14} />
          <span>
            No settings token is set, so writes are open to anyone who can reach this page. Set
            {' '}<b>Settings token</b> under Security to lock writes down.
          </span>
        </div>
      )}

      {msg && (
        <div className={'set-note ' + (msg.kind === 'ok' ? 'ok' : 'err')}>
          {msg.kind === 'ok' ? <Check size={14} /> : <AlertTriangle size={14} />}
          <span>{msg.text}</span>
        </div>
      )}

      {pending.length > 0 && (
        <div className="set-note restart">
          <Power size={14} />
          <span>Pending restart: {restartFields.join(', ')}.</span>
          <button className="set-mini" onClick={restart} disabled={busy}>Restart daemon</button>
        </div>
      )}

      {!snap && <div className="muted" style={{ padding: 24 }}>loading…</div>}

      {snap && (
        <div className="set-grid">
          {snap.sections.map((s) => (
            <section className="set-card" key={s.id} id={`set-${s.id}`}>
              <h5>{s.label}</h5>
              <p className="set-desc">{s.description}</p>
              <div className="set-fields">{s.fields.map(renderField)}</div>
            </section>
          ))}
        </div>
      )}
    </div>
  )
}
