import React from 'react'
import { createRoot } from 'react-dom/client'
import '@fontsource/inter/400.css'
import '@fontsource/inter/500.css'
import '@fontsource/inter/600.css'
import '@fontsource/inter/700.css'
import '@fontsource/jetbrains-mono/400.css'
import '@fontsource/jetbrains-mono/500.css'
import './index.css'
import BrainApp from './brain/BrainApp'

createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <BrainApp />
  </React.StrictMode>,
)

// Register the app-shell service worker so the installed PWA (Add to Home
// Screen) opens fast and offline-ish. Service workers require a secure context,
// so this runs on HTTPS or localhost and is
// a harmless no-op over plain-HTTP LAN; skipped in dev to avoid caching modules.
const secure = location.protocol === 'https:' || ['localhost', '127.0.0.1'].includes(location.hostname)
if ('serviceWorker' in navigator && secure) {
  window.addEventListener('load', () => {
    navigator.serviceWorker.register('/sw.js').catch(() => {})
  })
}
