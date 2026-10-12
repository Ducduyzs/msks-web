/// <reference types="vitest/config" />
import tailwindcss from '@tailwindcss/vite'
import react from '@vitejs/plugin-react'
import { defineConfig, loadEnv, type Plugin } from 'vite'
import { buildCsp, securityHeaders } from './src/lib/csp.ts'

/**
 * Bảo mật bản build: chèn CSP vào index.html (lớp dự phòng khi máy chủ tĩnh không đặt header) và xuất
 * `_headers` (Netlify / Cloudflare Pages) với đủ header — gồm `frame-ancestors`, thứ chỉ có hiệu lực qua HTTP.
 * Dev server không đặt CSP: HMR của Vite cần script inline.
 */
function securityPlugin(supabaseUrl: string | undefined, https: boolean): Plugin {
  return {
    name: 'msks-security-headers',
    apply: 'build',
    transformIndexHtml() {
      return [{ tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: buildCsp(supabaseUrl, { header: false, https }) }, injectTo: 'head-prepend' }]
    },
    generateBundle() {
      const lines = Object.entries(securityHeaders(supabaseUrl, https)).map(([name, value]) => `  ${name}: ${value}`)
      this.emitFile({ type: 'asset', fileName: '_headers', source: `/*\n${lines.join('\n')}\n` })
    },
  }
}

// https://vite.dev/config/
export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), 'VITE_')
  const https = (env.VITE_PUBLIC_ORIGIN ?? '').startsWith('https://')
  const headers = securityHeaders(env.VITE_SUPABASE_URL, https)
  const { 'Content-Security-Policy': _csp, ...devHeaders } = headers
  return {
    plugins: [react(), tailwindcss(), securityPlugin(env.VITE_SUPABASE_URL, https)],
    server: {
      port: 5173,
      headers: devHeaders,
      // Khi chạy VITE_API_MODE=http, /api được chuyển tới backend FastAPI (cùng origin → cookie phiên hoạt động).
      proxy: {
        '/api': { target: 'http://localhost:8000', changeOrigin: true },
      },
    },
    preview: {
      port: 4173,
      headers,
      proxy: {
        '/api': { target: 'http://localhost:8000', changeOrigin: true },
      },
    },
    build: {
      sourcemap: false, // không phát hành mã nguồn gốc kèm bản build
    },
    test: {
      // Test luôn chạy trên API mock, không phụ thuộc .env.local của máy dev.
      env: { VITE_API_MODE: 'mock' },
    },
  }
})
