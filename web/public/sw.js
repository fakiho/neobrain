/* Observatory service worker — a deliberately small, safe app-shell cache.
 *
 * Goals: the installed PWA opens instantly on repeat visits and survives a
 * flaky LAN; the API is never cached or interfered with.
 *
 * Strategy
 *   • /api/*            → network only (never cached, never touched)
 *   • navigations       → network-first, fall back to the cached shell when offline
 *   • hashed static     → stale-while-revalidate (app assets under /assets/*)
 *   • other same-origin → pass through to the network
 */
const CACHE = 'observatory-shell-v1'

const SHELL = [
  '/',
  '/index.html',
  '/manifest.webmanifest',
  '/favicon.svg',
  '/apple-touch-icon.png',
  '/icons/icon-180.png',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
]

self.addEventListener('install', (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .catch(() => {})
      .then(() => self.skipWaiting()),
  )
})

self.addEventListener('activate', (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  )
})

const isHashedAsset = (url) => url.pathname.startsWith('/assets/')

self.addEventListener('fetch', (event) => {
  const req = event.request
  if (req.method !== 'GET') return

  let url
  try {
    url = new URL(req.url)
  } catch {
    return
  }
  if (url.origin !== self.location.origin) return
  // The live API must always hit the network.
  if (url.pathname.startsWith('/api/')) return

  // Full-page loads: try the network, fall back to the cached shell offline.
  if (req.mode === 'navigate') {
    event.respondWith(
      fetch(req)
        .then((res) => {
          const copy = res.clone()
          caches.open(CACHE).then((c) => c.put('/index.html', copy)).catch(() => {})
          return res
        })
        .catch(() => caches.match('/index.html').then((hit) => hit || Response.error())),
    )
    return
  }

  if (!isHashedAsset(url)) return

  // Hashed build assets: serve from cache immediately, refresh in background.
  event.respondWith(
    caches.match(req).then((hit) => {
      const network = fetch(req)
        .then((res) => {
          if (res && res.ok) {
            const copy = res.clone()
            caches.open(CACHE).then((c) => c.put(req, copy)).catch(() => {})
          }
          return res
        })
        .catch(() => hit)
      return hit || network
    }),
  )
})
