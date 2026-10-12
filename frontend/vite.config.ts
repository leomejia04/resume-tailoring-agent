import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

// In development the browser talks only to Vite (one origin); Vite forwards
// /api to FastAPI. Same-origin means no CORS is needed.
// DEV_API_PORT runs a second local stack beside the default one (README).
const env = (globalThis as { process?: { env: Record<string, string | undefined> } }).process?.env ?? {}
const apiPort = /^\d{2,5}$/.test(env.DEV_API_PORT ?? '') ? env.DEV_API_PORT : '8000'
const apiProxy = {
  '/api': { target: `http://127.0.0.1:${apiPort}`, changeOrigin: false },
}

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy: apiProxy },
  preview: { host: '127.0.0.1', port: 4173, strictPort: true, proxy: apiProxy },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    css: false,
    restoreMocks: true,
    unstubGlobals: true,
  },
})
