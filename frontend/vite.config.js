import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { fileURLToPath } from 'node:url'

// In sviluppo le chiamate /api vengono girate al backend FastAPI su 8000.
// VITE_BACKEND_URL permette ai test UI di puntare un backend su un'altra
// porta senza toccare questo file.
const backend = process.env.VITE_BACKEND_URL || 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    port: 5173,
    proxy: {
      '/api': backend,
    },
  },
})
