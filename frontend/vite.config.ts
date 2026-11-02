import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  // Dev only: proxy API calls to a local backend.
  server: {
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
      '^/p/[^/]+/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
})
