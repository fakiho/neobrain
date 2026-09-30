export type NodeKind = 'hub' | 'atom'

export interface BrainNode {
  id: string
  label: string
  kind: NodeKind
  type: string // hub id for hubs, atom type for atoms
  created: number
  text?: string
  weight?: number
  hub?: string // primary hub (atoms)
  source?: string // where it was memorized from
  tags?: string[]
}

export interface BrainLink {
  source: string
  target: string
  type: string
}
