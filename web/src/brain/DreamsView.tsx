import { useEffect, useState } from 'react'
import { Moon, Sparkles } from 'lucide-react'

interface Dream {
  date: string
  narrative: string | null
  narrative_source: string | null
  light: string | null
  deep: string | null
  nights_files: { rem: boolean; light: boolean; deep: boolean }
  lessons: { id: string; label: string; hub: string | null }[]
  promoted: { id: string; label: string; hub: string | null }[]
  node_id: string | null
  node_text: string | null
}

export function DreamsView({ onOpen }: { onOpen: (id: string) => void }) {
  const [dreams, setDreams] = useState<Dream[]>([])
  const [loading, setLoading] = useState(true)
  const [openLight, setOpenLight] = useState<string | null>(null)

  useEffect(() => {
    let alive = true
    const load = () =>
      fetch('/api/mind/dreams')
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error('x'))))
        .then((d) => {
          if (!alive) return
          setDreams(d.dreams ?? [])
          setLoading(false)
        })
        .catch(() => alive && setLoading(false))
    load()
    const t = window.setInterval(load, 30_000)
    return () => { alive = false; window.clearInterval(t) }
  }, [])

  return (
    <div className="dreams">
      <header className="dreams-head">
        <div>
          <h1>
            <Moon size={18} style={{ verticalAlign: -3, marginRight: 8, color: 'var(--text-muted)' }} />
            Dreams
          </h1>
          <p className="muted">
            What the agent consolidated while it slept — {dreams.length} night{dreams.length === 1 ? '' : 's'} on record.
            Open a night in the Brain to see what it touched.
          </p>
        </div>
      </header>

      {loading && <div className="muted" style={{ padding: 20 }}>loading…</div>}
      {!loading && dreams.length === 0 && (
        <div className="muted" style={{ padding: 20 }}>No dreams yet. The routine runs nightly at 03:30.</div>
      )}

      <div className="nights">
        {dreams.map((d) => (
          <article className="night" key={d.date}>
            <div className="night-head">
              <span className="night-date mono">{d.date}</span>
              <span className="night-files">
                <span className={'flag' + (d.nights_files.light ? ' on' : '')}>light</span>
                <span className={'flag' + (d.nights_files.rem ? ' on' : '')}>rem</span>
                <span className={'flag' + (d.nights_files.deep ? ' on' : '')}>deep</span>
              </span>
              <div style={{ flex: 1 }} />
              {d.node_id && (
                <button className="obs-btn" onClick={() => onOpen(d.node_id!)}>
                  <Sparkles size={13} /> Open in Brain
                </button>
              )}
            </div>

            {d.narrative && (
              <>
                <div className="obs-sec" style={{ marginTop: 4 }}>
                  The dream{d.narrative_source ? ` · ${d.narrative_source}` : ''}
                </div>
                <blockquote className="dream-text">{d.narrative}</blockquote>
              </>
            )}

            {(d.lessons.length > 0 || d.promoted.length > 0) && (
              <div className="night-cols">
                {d.lessons.length > 0 && (
                  <div className="night-col">
                    <div className="obs-sec">Durable truths</div>
                    {d.lessons.map((l) => (
                      <div className="dream-line" key={l.id}>
                        <span className="dot" style={{ background: '#7ba38c' }} />
                        <span>{l.label}</span>
                        {l.hub && <span className="hubchip">{l.hub}</span>}
                      </div>
                    ))}
                  </div>
                )}
                {d.promoted.length > 0 && (
                  <div className="night-col">
                    <div className="obs-sec">Patterns promoted</div>
                    {d.promoted.map((l) => (
                      <div className="dream-line" key={l.id}>
                        <span className="dot" style={{ background: '#948ebd' }} />
                        <span>{l.label}</span>
                        {l.hub && <span className="hubchip">{l.hub}</span>}
                      </div>
                    ))}
                  </div>
                )}
              </div>
            )}

            <div className="night-foot">
              {d.node_text && <span className="muted">{d.node_text}</span>}
              {d.light && (
                <button className="obs-btn" onClick={() => setOpenLight(openLight === d.date ? null : d.date)}>
                  {openLight === d.date ? 'Hide' : 'Show'} the day’s summary
                </button>
              )}
            </div>
            {openLight === d.date && d.light && (
              <pre className="dream-summary">{d.light}</pre>
            )}
          </article>
        ))}
      </div>
    </div>
  )
}
