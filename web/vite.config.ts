import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Dev: `npm run dev` proxies /api to the neoBrain daemon API on :9192.
// Prod: `npm run build` emits web/dist, served by FastAPI at /.
export default defineConfig({
  plugins: [react()],
  build: { outDir: 'dist', emptyOutDir: true },
  server: {
    port: 5199,
    proxy: { '/api': 'http://127.0.0.1:9192' },
  },
})
