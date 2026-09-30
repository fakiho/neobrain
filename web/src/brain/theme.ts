export const HUB_COLOR = '#4a5064'

export const ATOM_COLORS: Record<string, string> = {
  observation: '#8fa3b8',
  decision: '#b39262',
  lesson: '#7ba38c',
  preference: '#948ebd',
  identity: '#c9a9c4',
  event: '#7e8393',
  dream: '#a488b6',
  open: '#b5828a',
}

export const ATOM_LABELS: Record<string, string> = {
  observation: 'Observation',
  decision: 'Decision',
  lesson: 'Lesson',
  preference: 'Rules',
  identity: 'Identity',
  event: 'Event',
  dream: 'Dream',
  open: 'Open loop',
}

export const EDGE_COLORS: Record<string, string> = {
  about: '#3a4152',
  supersedes: '#b39262',
  'caused-by': '#b5828a',
  'derived-from': '#7ba38c',
  consolidates: '#948ebd',
}

export const EDGE_LABELS: Record<string, string> = {
  about: 'about',
  supersedes: 'supersedes',
  'caused-by': 'caused by',
  'derived-from': 'derived from',
  consolidates: 'consolidates',
}
