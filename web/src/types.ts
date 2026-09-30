export interface TEvent {
  id: string
  ts: number
  ts_end: number | null
  source: string
  lane: string
  category: string
  actor: string
  title: string
  detail: string | null
  status: string | null
  severity: string
  project_id: string | null
  session_id: string | null
  refs: Record<string, any>
  raw: any
  ts_iso?: string
  ts_end_iso?: string
}

export interface Stats {
  total: number
  by_source: Record<string, number>
  by_lane: Record<string, number>
  by_category: Record<string, number>
  by_severity: Record<string, number>
  range: { min: number; max: number; min_iso?: string; max_iso?: string }
  doc_versions: number
  links: number
  sessions: number
}

export interface Session {
  id: string
  project_id: string | null
  directory: string | null
  title: string | null
  agent: string | null
  model: string | null
  cost: number | null
  ts_created: number
  ts_updated: number
  ts_created_iso?: string
}

export interface ChainLink {
  from_id: string
  to_id: string
  kind: string
  event: TEvent | null
}

export interface Chain {
  event: TEvent
  in: ChainLink[]
  out: ChainLink[]
}

export interface EventFilters {
  start?: number
  end?: number
  day?: string
  lane?: string
  source?: string
  category?: string
  project?: string
  session?: string
  q?: string
  limit?: number
  offset?: number
}
