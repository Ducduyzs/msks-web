// Tạo CSP và header bảo mật — thuần TypeScript (không DOM) để cả vite.config.ts lẫn ứng dụng dùng chung.

/**
 * Content-Security-Policy cho bản build: chỉ chạy script của chính ứng dụng; ảnh/âm thanh/kết nối chỉ tới
 * origin của app và Supabase (Auth, Storage URL ký). `frame-ancestors` chỉ có hiệu lực qua header HTTP.
 */
export function buildCsp(supabaseUrl: string | undefined, options: { header: boolean; https: boolean }) {
  const supabase = supabaseUrl ? new URL(supabaseUrl).origin : ''
  const directives = [
    "default-src 'self'",
    "script-src 'self'",
    "style-src 'self'",
    `img-src 'self' data: blob: ${supabase}`.trim(),
    `media-src 'self' blob: ${supabase}`.trim(),
    `connect-src 'self' ${supabase}`.trim(),
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-src 'none'",
    "worker-src 'self'",
    "manifest-src 'self'",
  ]
  if (options.header) directives.push("frame-ancestors 'none'")
  if (options.https) directives.push('upgrade-insecure-requests')
  return directives.join('; ')
}

/** Header bảo mật cho máy chủ phục vụ bản build (vite preview, Netlify/Cloudflare `_headers`, nginx). */
export function securityHeaders(supabaseUrl: string | undefined, https: boolean): Record<string, string> {
  return {
    'Content-Security-Policy': buildCsp(supabaseUrl, { header: true, https }),
    'X-Frame-Options': 'DENY',
    'X-Content-Type-Options': 'nosniff',
    'Referrer-Policy': 'no-referrer',
    'Permissions-Policy': 'camera=(), microphone=(), geolocation=(), payment=(), usb=()',
    'Cross-Origin-Opener-Policy': 'same-origin',
    ...(https ? { 'Strict-Transport-Security': 'max-age=31536000; includeSubDomains' } : {}),
  }
}
