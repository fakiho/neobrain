export interface LaneMeta {
  key: string
  label: string
  color: string
  short: string
  description: string
}

// Order = display order top-to-bottom on the timeline.
export const LANES: LaneMeta[] = [
  { key: 'request', label: 'Requests', color: '#818cf8', short: 'REQ', description: 'User prompts / asks' },
  { key: 'decision', label: 'Decisions', color: '#f5a524', short: 'DEC', description: 'Decisions, root causes, lessons' },
  { key: 'action', label: 'Actions', color: '#22d3ee', short: 'ACT', description: 'Shell commands, skills, code' },
  { key: 'file', label: 'File changes', color: '#a78bfa', short: 'FIL', description: 'Reads, writes, edits' },
  { key: 'research', label: 'Research', color: '#f472b6', short: 'RES', description: 'Web fetch / search' },
  { key: 'question', label: 'Questions', color: '#facc15', short: 'QST', description: 'Questions asked' },
  { key: 'git', label: 'Git', color: '#34d399', short: 'GIT', description: 'Commits across repos' },
  { key: 'doc', label: 'Docs & memory', color: '#60a5fa', short: 'DOC', description: 'INFRA / memory / identity versions' },
  { key: 'dream', label: 'Dreams', color: '#a5b4fc', short: 'DRM', description: 'Dream diary entries' },
  { key: 'notice', label: 'Notices', color: '#64748b', short: 'NTC', description: 'System, model, rationale' },
]

export const LANE_BY_KEY: Record<string, LaneMeta> = Object.fromEntries(
  LANES.map((l) => [l.key, l]),
)

export function laneMeta(key: string): LaneMeta {
  return LANE_BY_KEY[key] ?? { key, label: key, color: '#94a3b8', short: '—', description: '' }
}

export function laneColor(key: string): string {
  return laneMeta(key).color
}

export const SEVERITY_COLOR: Record<string, string> = {
  info: '#64748b',
  notice: '#60a5fa',
  success: '#34d399',
  warning: '#f59e0b',
  error: '#ef4444',
}

export function severityColor(sev?: string | null): string {
  return SEVERITY_COLOR[sev ?? 'info'] ?? SEVERITY_COLOR.info
}

export const SOURCE_COLOR: Record<string, string> = {
  opencode: '#22d3ee',
  git: '#34d399',
  docs: '#60a5fa',
}
