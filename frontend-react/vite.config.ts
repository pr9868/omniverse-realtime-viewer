import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vitejs.dev/config/
export default defineConfig({
  plugins: [react()],

  server: {
    port: 5173,
    // Proxy /api/* and /healthz to the Python server during development.
    // This way fetch('/api/scenes') in your React code works without
    // hardcoding the server IP — Vite forwards it for you.
    // Set VITE_SERVER_IP in .env.local to point at a remote server,
    // or leave unset to proxy to localhost.
    proxy: {
      '/api': {
        target: `http://${process.env.VITE_SERVER_IP ?? 'localhost'}:8081`,
        changeOrigin: true,
      },
      '/healthz': {
        target: `http://${process.env.VITE_SERVER_IP ?? 'localhost'}:8081`,
        changeOrigin: true,
      },
      '/camera': {
        target: `http://${process.env.VITE_SERVER_IP ?? 'localhost'}:8081`,
        changeOrigin: true,
      },
    },
  },

  build: {
    outDir: 'dist',
    emptyOutDir: true,
  },
})
