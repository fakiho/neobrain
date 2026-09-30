import { Braces, FileText } from 'lucide-react'
import { DocCards } from './DocCards'

export type ReaderGroup = 'memory' | 'identity' | 'infra'

const META: Record<ReaderGroup, { title: string; icon: any; blurb: string }> = {
  memory: {
    title: 'Memory',
    icon: FileText,
    blurb: '',
  },
  identity: {
    title: 'Identity & rules',
    icon: Braces,
    blurb: 'Who the agent is, its personality and its standing rules (USER, SOUL, IDENTITY, AGENTS).',
  },
  infra: {
    title: 'Infrastructure docs',
    icon: FileText,
    blurb: 'The INFRA_* reference docs — the map of the machine.',
  },
}

/** Doc-card reader for the Identity and Infra tabs (Memory has its own view). */
export function ReaderView({ group, onOpen }: { group: ReaderGroup; onOpen: (id: string) => void }) {
  const meta = META[group]
  const Icon = meta.icon
  return (
    <div className="dreams">
      <header className="dreams-head">
        <h1><Icon size={18} style={{ verticalAlign: -3, marginRight: 8, color: 'var(--text-muted)' }} />{meta.title}</h1>
        {meta.blurb && <p className="muted">{meta.blurb}</p>}
      </header>
      <DocCards group={group} onOpen={onOpen} />
    </div>
  )
}
