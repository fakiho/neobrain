import { useEffect, useState } from 'react'

export function useThemeAttr(): 'dark' | 'light' {
  const [theme, setTheme] = useState<'dark' | 'light'>(
    (document.documentElement.dataset.theme as 'dark' | 'light') || 'dark',
  )
  useEffect(() => {
    const obs = new MutationObserver(() => {
      setTheme((document.documentElement.dataset.theme as 'dark' | 'light') || 'dark')
    })
    obs.observe(document.documentElement, { attributes: true, attributeFilter: ['data-theme'] })
    return () => obs.disconnect()
  }, [])
  return theme
}
