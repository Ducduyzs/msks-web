import { useQuery, useQueryClient } from '@tanstack/react-query'
import { Layers, LogIn, UserPlus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { api } from '../api'
import { ErrorBox, Loading, Notice, Spinner } from '../components/ui'
import { AuthContext, clearServerSession, exchangeToken, fetchMe, supabase } from './session'

/** Chặn ứng dụng sau màn hình đăng nhập khi chạy với backend thật. Chế độ mock không cần đăng nhập. */
export function AuthGate({ children }: { children: ReactNode }) {
  if (api.mode === 'mock') return <AuthContext.Provider value={{ user: null }}>{children}</AuthContext.Provider>
  return <RealAuthGate>{children}</RealAuthGate>
}

function RealAuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const me = useQuery({ queryKey: ['auth', 'me'], queryFn: fetchMe, retry: false, staleTime: 60_000 })

  // Mỗi khi Supabase cấp hoặc làm mới token, đổi lại cookie phiên ở backend.
  useEffect(() => {
    if (!supabase) return
    const { data } = supabase.auth.onAuthStateChange((event, session) => {
      if (session && ['INITIAL_SESSION', 'SIGNED_IN', 'TOKEN_REFRESHED'].includes(event)) {
        exchangeToken(session.access_token)
          .then(() => queryClient.invalidateQueries({ queryKey: ['auth', 'me'] }))
          .catch(() => undefined)
      }
    })
    return () => data.subscription.unsubscribe()
  }, [queryClient])

  const signOut = useCallback(async () => {
    await supabase?.auth.signOut()
    await clearServerSession()
    queryClient.clear()
    await queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
  }, [queryClient])

  const value = useMemo(() => ({ user: me.data ?? null, signOut }), [me.data, signOut])

  if (!supabase) {
    return (
      <Centered>
        <Notice tone="warn" title="Thiếu cấu hình Supabase">
          Đặt VITE_SUPABASE_URL và VITE_SUPABASE_PUBLISHABLE_KEY trong web/.env.local rồi khởi động lại Vite.
        </Notice>
      </Centered>
    )
  }
  if (me.isPending) return <Centered><Loading label="Đang kiểm tra phiên đăng nhập…" /></Centered>
  if (me.error) return <Centered><ErrorBox error={me.error} onRetry={() => me.refetch()} /></Centered>
  if (!me.data) return <LoginPage />
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex min-h-full max-w-sm flex-col justify-center px-4 py-16">{children}</div>
}

function LoginPage() {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'signin' | 'signup'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!supabase) return
    setBusy(true)
    setError(null)
    setInfo(null)
    try {
      const result =
        mode === 'signin'
          ? await supabase.auth.signInWithPassword({ email, password })
          : await supabase.auth.signUp({ email, password })
      if (result.error) throw result.error
      if (!result.data.session) {
        setInfo('Đã tạo tài khoản. Kiểm tra email để xác nhận rồi đăng nhập.')
        setMode('signin')
        return
      }
      await exchangeToken(result.data.session.access_token)
      await queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Centered>
      <div className="mb-6 flex items-center gap-2 text-lg font-semibold">
        <Layers className="size-6 text-accent" /> MSKS
      </div>
      <form onSubmit={submit} className="card space-y-4 p-6">
        <h1 className="text-lg font-semibold">{mode === 'signin' ? 'Đăng nhập' : 'Tạo tài khoản'}</h1>
        <div className="space-y-1">
          <label htmlFor="email" className="label">Email</label>
          <input id="email" type="email" required autoComplete="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
        </div>
        <div className="space-y-1">
          <label htmlFor="password" className="label">Mật khẩu</label>
          <input
            id="password"
            type="password"
            required
            minLength={8}
            autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
            className="input"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
          />
        </div>
        {error && <ErrorBox error={new Error(error)} />}
        {info && <Notice>{info}</Notice>}
        <button className="btn btn-primary w-full" disabled={busy}>
          {busy ? <Spinner /> : mode === 'signin' ? <LogIn className="size-4" /> : <UserPlus className="size-4" />}
          {mode === 'signin' ? 'Đăng nhập' : 'Tạo tài khoản'}
        </button>
        <button
          type="button"
          className="w-full text-center text-sm text-accent hover:underline"
          onClick={() => setMode(mode === 'signin' ? 'signup' : 'signin')}
        >
          {mode === 'signin' ? 'Chưa có tài khoản? Tạo tài khoản' : 'Đã có tài khoản? Đăng nhập'}
        </button>
      </form>
    </Centered>
  )
}
