import { useQuery } from '@tanstack/react-query'
import type { Chain, EventFilters, Session, Stats, TEvent } from './types'

async function get<T>(path: string, params?: Record<string, any>): Promise<T> {
  const qs = params
    ? '?' +
      new URLSearchParams(
        Object.entries(params)
          .filter(([, v]) => v !== undefined && v !== null && v !== '')
          .map(([k, v]) => [k, String(v)]),
      ).toString()
    : ''
  const res = await fetch(`/api${path}${qs}`)
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} — ${path}`)
  return res.json() as Promise<T>
}

export const api = {
  stats: () => get<Stats>('/stats'),
  events: (f: EventFilters) => get<{ count: number; events: TEvent[] }>('/events', f),
  event: (id: string) => get<TEvent>(`/events/${id}`),
  chain: (id: string) => get<Chain>(`/chain/${id}`),
  sessions: () => get<{ count: number; sessions: Session[] }>('/sessions'),
  session: (id: string) => get<{ count: number; events: TEvent[] }>(`/sessions/${id}`),
  search: (q: string, limit = 100) => get<{ count: number; events: TEvent[] }>('/search', { q, limit }),
  health: () => get<{ status: string; events: number }>('/health'),
}

export const qk = {
  stats: ['stats'] as const,
  events: (f: EventFilters) => ['events', f] as const,
  event: (id: string) => ['event', id] as const,
  chain: (id: string) => ['chain', id] as const,
  sessions: ['sessions'] as const,
  session: (id: string) => ['session', id] as const,
  search: (q: string) => ['search', q] as const,
}

export function useStats() {
  return useQuery({ queryKey: qk.stats, queryFn: api.stats, staleTime: 15_000 })
}

export function useEvents(f: EventFilters, refetchInterval: number | false = false) {
  return useQuery({
    queryKey: qk.events(f),
    queryFn: () => api.events(f),
    staleTime: 10_000,
    refetchInterval,
  })
}

export function useEvent(id: string | null) {
  return useQuery({
    queryKey: qk.event(id ?? ''),
    queryFn: () => api.event(id!),
    enabled: !!id,
  })
}

export function useChain(id: string | null) {
  return useQuery({
    queryKey: qk.chain(id ?? ''),
    queryFn: () => api.chain(id!),
    enabled: !!id,
  })
}

export function useSessions() {
  return useQuery({ queryKey: qk.sessions, queryFn: api.sessions, staleTime: 30_000 })
}

export function useSearch(q: string) {
  return useQuery({
    queryKey: qk.search(q),
    queryFn: () => api.search(q),
    enabled: q.trim().length > 1,
    staleTime: 30_000,
  })
}
