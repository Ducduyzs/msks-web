// Đăng nhập Supabase phía trình duyệt + đổi access token lấy cookie phiên của backend.
// Chỉ dùng khóa publishable (công khai); khóa secret không bao giờ ở frontend.
import { createClient, type SupabaseClient } from '@supabase/supabase-js'
import { createContext, useContext } from 'react'
import { ApiError } from '../api/client'
import { t } from '../i18n'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
const apiBase = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '')

export const supabase: SupabaseClient | null = url && key ? createClient(url, key) : null

export interface SessionUser {
  id: string
  email: string | null
  /** aal2 = phiên đã qua xác thực hai lớp. */
  aal?: 'aal1' | 'aal2'
  mfa_enabled?: boolean
  expires_at?: number
}

export interface SecurityEvent {
  event: string
  created_at: string
  ip: string | null
  user_agent: string | null
  detail: Record<string, unknown>
}

function readCookie(name: string) {
  return document.cookie
    .split('; ')
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1)
}

async function errorFrom(response: Response): Promise<ApiError> {
  try {
    const body = await response.json()
    if (body?.code) return new ApiError(response.status, body)
  } catch {
    /* thân không phải JSON */
  }
  return new ApiError(response.status, { code: `http_${response.status}`, message: t('Máy chủ từ chối phiên đăng nhập.'), retryable: response.status >= 500 })
}

export async function fetchMe(): Promise<SessionUser | null> {
  const response = await fetch(`${apiBase}/auth/me`, { credentials: 'same-origin' })
  if (response.status === 401) return null
  if (!response.ok) throw new Error(t('Không kiểm tra được phiên ({0}).', { 0: response.status }))
  return (await response.json()) as SessionUser
}

/** Backend kiểm tra JWT qua JWKS, phiên còn sống và 2FA rồi đặt cookie HttpOnly + csrf_token. */
export async function exchangeToken(accessToken: string): Promise<void> {
  const response = await fetch(`${apiBase}/auth/session`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ access_token: accessToken }),
  })
  if (!response.ok) throw await errorFrom(response)
}

/** Thu hồi phiên ở server (token cũ hết hiệu lực trong ≤ 15 s) rồi xóa cookie. `global` = mọi thiết bị. */
export async function clearServerSession(scope: 'local' | 'global' = 'local'): Promise<void> {
  const csrf = readCookie('csrf_token')
  const response = await fetch(`${apiBase}/auth/session?scope=${scope}`, {
    method: 'DELETE',
    credentials: 'same-origin',
    headers: csrf ? { 'X-CSRF-Token': decodeURIComponent(csrf) } : {},
  }).catch(() => null)
  if (scope === 'global' && (!response || !response.ok)) throw new Error(t('Không đăng xuất được các thiết bị khác.'))
}

export async function fetchSecurityEvents(): Promise<SecurityEvent[]> {
  const response = await fetch(`${apiBase}/auth/security-events`, { credentials: 'same-origin' })
  if (!response.ok) throw await errorFrom(response)
  return ((await response.json()) as { items: SecurityEvent[] }).items
}

export interface AuthState {
  user: SessionUser | null
  signOut?: (options?: { scope?: 'local' | 'global'; reason?: string }) => Promise<void>
  /** Lý do lần đăng xuất gần nhất (hết thời gian chờ, bị đăng xuất ở nơi khác…). */
  notice?: string | null
}

export const AuthContext = createContext<AuthState>({ user: null })

export const useAuth = () => useContext(AuthContext)
