// Đăng nhập Supabase phía trình duyệt + đổi access token lấy cookie phiên của backend.
// Chỉ dùng khóa publishable (công khai); khóa secret không bao giờ ở frontend.
import { createClient, type SupabaseClient } from '@supabase/supabase-js'
import { createContext, useContext } from 'react'

const url = import.meta.env.VITE_SUPABASE_URL
const key = import.meta.env.VITE_SUPABASE_PUBLISHABLE_KEY
const apiBase = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '')

export const supabase: SupabaseClient | null = url && key ? createClient(url, key) : null

export interface SessionUser {
  id: string
  email: string | null
}

export async function fetchMe(): Promise<SessionUser | null> {
  const response = await fetch(`${apiBase}/auth/me`, { credentials: 'same-origin' })
  if (response.status === 401) return null
  if (!response.ok) throw new Error(`Không kiểm tra được phiên (${response.status}).`)
  return (await response.json()) as SessionUser
}

/** Backend kiểm tra JWT qua JWKS rồi đặt cookie HttpOnly + csrf_token. */
export async function exchangeToken(accessToken: string): Promise<void> {
  const response = await fetch(`${apiBase}/auth/session`, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ access_token: accessToken }),
  })
  if (!response.ok) throw new Error('Máy chủ từ chối phiên đăng nhập.')
}

export async function clearServerSession(): Promise<void> {
  await fetch(`${apiBase}/auth/session`, { method: 'DELETE', credentials: 'same-origin' }).catch(() => undefined)
}

export interface AuthState {
  user: SessionUser | null
  signOut?: () => Promise<void>
}

export const AuthContext = createContext<AuthState>({ user: null })

export const useAuth = () => useContext(AuthContext)
