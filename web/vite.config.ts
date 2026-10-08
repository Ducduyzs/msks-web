/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // Khi chạy VITE_API_MODE=http, /api được chuyển tới backend FastAPI (cùng origin → cookie phiên hoạt động).
    proxy: {
      '/api': { target: 'http://localhost:8000', changeOrigin: true },
    },
  },
  test: {
    // Test luôn chạy trên API mock, không phụ thuộc .env.local của máy dev.
    env: { VITE_API_MODE: 'mock' },
  },
})
