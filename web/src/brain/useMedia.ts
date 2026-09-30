import { useEffect, useState } from 'react'

// Reactive matchMedia hook. SSR-safe (returns false when window is absent) and
// updates the component whenever the query's match state changes (e.g. rotate
// an iPad, or resize a desktop window across the mobile breakpoint).
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState<boolean>(() =>
    typeof window !== 'undefined' && 'matchMedia' in window ? window.matchMedia(query).matches : false,
  )

  useEffect(() => {
    if (typeof window === 'undefined' || !('matchMedia' in window)) return
    const mql = window.matchMedia(query)
    const onChange = () => setMatches(mql.matches)
    onChange()
    mql.addEventListener('change', onChange)
    return () => mql.removeEventListener('change', onChange)
  }, [query])

  return matches
}

// Small viewport → render the mobile shell (drawer + bottom sheet).
// Desktop widths are untouched. Coarse-pointer tablets in landscape can exceed
// this width, but they are still handled by the touch gesture path in the canvas.
export const MOBILE_QUERY = '(max-width: 900px)'

export function useIsMobile(): boolean {
  // Small viewports OR any coarse pointer (touch). A touch iPad in landscape
  // then gets the mobile shell too — the desktop mouse path is unaffected.
  const small = useMediaQuery(MOBILE_QUERY)
  const coarse = useMediaQuery('(pointer: coarse)')
  return small || coarse
}
