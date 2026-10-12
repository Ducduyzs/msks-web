import { useQuery, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Layers, LogIn, UserPlus } from 'lucide-react'
import { useCallback, useEffect, useMemo, useState, type FormEvent, type ReactNode } from 'react'
import { ApiError, api } from '../api'
import { PreferencesMenu } from '../components/PreferencesMenu'
import { ErrorBox, Loading, Notice, Spinner } from '../components/ui'
import { PASSWORD_MIN_LENGTH, UNAUTHORIZED_EVENT, idleLogoutMs, passwordProblems } from '../lib/security'
import { AuthContext, clearServerSession, exchangeToken, fetchMe, supabase } from './session'
import { useIdleLogout } from './useIdleLogout'
import { t, msg, translated } from '../i18n'

/** Chặn ứng dụng sau màn hình đăng nhập khi chạy với backend thật. Chế độ mock không cần đăng nhập. */
export function AuthGate({ children }: { children: ReactNode }) {
  if (api.mode === 'mock') return <AuthContext.Provider value={{ user: null }}>{children}</AuthContext.Provider>
  return <RealAuthGate>{children}</RealAuthGate>
}

const UNAUTHORIZED_REASON: Record<string, string> = translated({
  session_revoked: msg('Phiên này đã bị đăng xuất (có thể từ thiết bị khác). Hãy đăng nhập lại.'),
  mfa_required: msg('Tài khoản đã bật xác thực hai lớp: đăng nhập lại và nhập mã xác thực.'),
  unauthorized: msg('Phiên đăng nhập đã hết hạn. Hãy đăng nhập lại.'),
})

function RealAuthGate({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const me = useQuery({ queryKey: ['auth', 'me'], queryFn: fetchMe, retry: false, staleTime: 60_000 })
  const [notice, setNotice] = useState<string | null>(null)

  // Mỗi khi Supabase cấp hoặc làm mới token (kể cả sau khi qua 2FA), đổi lại cookie phiên ở backend.
  useEffect(() => {
    if (!supabase) return
    const { data } = supabase.auth.onAuthStateChange((event, session) => {
      if (session && ['INITIAL_SESSION', 'SIGNED_IN', 'TOKEN_REFRESHED', 'MFA_CHALLENGE_VERIFIED'].includes(event)) {
        exchangeToken(session.access_token)
          .then(() => queryClient.invalidateQueries({ queryKey: ['auth', 'me'] }))
          .catch(() => undefined)
      }
    })
    return () => data.subscription.unsubscribe()
  }, [queryClient])

  const signOut = useCallback(
    async (options: { scope?: 'local' | 'global'; reason?: string } = {}) => {
      try {
        await clearServerSession(options.scope ?? 'local')
      } finally {
        // Đăng xuất phía Supabase chỉ trên thiết bị này; phiên server đã bị thu hồi ở bước trên.
        await supabase?.auth.signOut({ scope: 'local' })
        setNotice(options.reason ?? null)
        // Về màn hình đăng nhập ngay rồi xóa dữ liệu workspace khỏi bộ nhớ đệm. (Trước đây gọi clear() rồi
        // invalidate ['auth','me']: sau clear() không còn query nào để invalidate nên giao diện vẫn ở trong app.)
        queryClient.setQueryData(['auth', 'me'], null)
        queryClient.removeQueries({ predicate: (query) => query.queryKey[0] !== 'auth' })
      }
    },
    [queryClient],
  )

  // API trả 401 ở bất kỳ đâu (phiên bị thu hồi ở thiết bị khác, cần 2FA, hết hạn) → về màn hình đăng nhập kèm lý do.
  useEffect(() => {
    const onUnauthorized = (event: Event) => {
      const code = (event as CustomEvent<string>).detail
      void signOut({ reason: UNAUTHORIZED_REASON[code] ?? UNAUTHORIZED_REASON.unauthorized })
    }
    window.addEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
    return () => window.removeEventListener(UNAUTHORIZED_EVENT, onUnauthorized)
  }, [signOut])

  const value = useMemo(() => ({ user: me.data ?? null, signOut, notice }), [me.data, signOut, notice])

  if (!supabase) {
    return (
      <Centered>
        <Notice tone="warn" title={t('Thiếu cấu hình Supabase')}>
          {t('Đặt VITE_SUPABASE_URL và VITE_SUPABASE_PUBLISHABLE_KEY trong web/.env.local rồi khởi động lại Vite.')}
        </Notice>
      </Centered>
    )
  }
  if (me.isPending) return <Centered><Loading label={t('Đang kiểm tra phiên đăng nhập…')} /></Centered>
  if (me.error) return <Centered><ErrorBox error={me.error} onRetry={() => me.refetch()} /></Centered>
  if (!me.data) return <LoginPage notice={notice} />
  return (
    <AuthContext.Provider value={value}>
      <IdleGuard onIdle={() => signOut({ reason: t('Đã tự đăng xuất vì không hoạt động. Hãy đăng nhập lại.') })} />
      {children}
    </AuthContext.Provider>
  )
}

function IdleGuard({ onIdle }: { onIdle: () => void }) {
  const { remainingMs, stay } = useIdleLogout(idleLogoutMs(), onIdle)
  if (remainingMs === null) return null
  return (
    <div role="alertdialog" aria-live="assertive" aria-label={t('Sắp tự đăng xuất')} className="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit max-w-[calc(100%-2rem)]">
      <div className="card flex flex-wrap items-center gap-3 border-contested/50 px-4 py-3 shadow-lg">
        <span className="text-sm">
          {t('Không có hoạt động — tự đăng xuất sau')} <strong>{Math.ceil(remainingMs / 1000)} {t('giây')}</strong>.
        </span>
        <button className="btn btn-primary" onClick={stay} autoFocus>
          {t('Tiếp tục làm việc')}
        </button>
      </div>
    </div>
  )
}

function Centered({ children }: { children: ReactNode }) {
  return <div className="mx-auto flex min-h-full max-w-sm flex-col justify-center px-4 py-16">{children}</div>
}

/** Đã đăng nhập bằng mật khẩu nhưng tài khoản bật 2FA → cần mã TOTP để nâng phiên lên aal2. */
async function needsMfa(): Promise<boolean> {
  if (!supabase) return false
  const { data } = await supabase.auth.mfa.getAuthenticatorAssuranceLevel()
  return data?.nextLevel === 'aal2' && data.currentLevel !== 'aal2'
}

function LoginPage({ notice }: { notice: string | null }) {
  const queryClient = useQueryClient()
  const [mode, setMode] = useState<'signin' | 'signup' | 'mfa'>('signin')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [code, setCode] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [info, setInfo] = useState<string | null>(null)
  const problems = mode === 'signup' && password ? passwordProblems(password, email) : []

  // Mở lại trang khi phiên Supabase còn nhưng chưa qua 2FA → vào thẳng bước nhập mã.
  useEffect(() => {
    void needsMfa().then((required) => required && setMode('mfa'))
  }, [])

  async function finish(accessToken: string) {
    await exchangeToken(accessToken)
    await queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
  }

  async function submit(event: FormEvent) {
    event.preventDefault()
    if (!supabase) return
    setBusy(true)
    setError(null)
    setInfo(null)
    try {
      if (mode === 'mfa') {
        const factors = await supabase.auth.mfa.listFactors()
        const factor = factors.data?.totp.find((f) => f.status === 'verified')
        if (!factor) throw new Error(t('Không tìm thấy ứng dụng xác thực đã đăng ký cho tài khoản này.'))
        const verified = await supabase.auth.mfa.challengeAndVerify({ factorId: factor.id, code: code.trim() })
        if (verified.error) throw new Error(t('Mã xác thực không đúng hoặc đã hết hạn.'))
        await finish(verified.data.access_token)
        return
      }
      if (mode === 'signup' && problems.length) throw new Error(t('Mật khẩu cần: {0}.', { 0: problems.join('; ') }))
      const result =
        mode === 'signin'
          ? await supabase.auth.signInWithPassword({ email, password })
          : await supabase.auth.signUp({ email, password })
      // Không nói rõ sai email hay sai mật khẩu (tránh dò tài khoản).
      if (result.error) throw new Error(mode === 'signin' ? t('Email hoặc mật khẩu không đúng.') : result.error.message)
      if (!result.data.session) {
        setInfo(t('Đã tạo tài khoản. Kiểm tra email để xác nhận rồi đăng nhập.'))
        setMode('signin')
        return
      }
      setPassword('')
      if (await needsMfa()) {
        setMode('mfa')
        return
      }
      await finish(result.data.session.access_token)
    } catch (err) {
      if (err instanceof ApiError && err.code === 'mfa_required') setMode('mfa')
      else setError(err instanceof Error ? err.message : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <Centered>
      <div className="mb-6 flex items-center gap-2 text-lg font-semibold">
        <Layers className="size-6 text-accent" /> MSKS
        <span className="ml-auto">
          <PreferencesMenu />
        </span>
      </div>
      {notice && (
        <div className="mb-4">
          <Notice tone="warn">{notice}</Notice>
        </div>
      )}
      <form onSubmit={submit} className="card space-y-4 p-6">
        <h1 className="text-lg font-semibold">{mode === 'signin' ? t('Đăng nhập') : mode === 'signup' ? t('Tạo tài khoản') : t('Xác thực hai lớp')}</h1>
        {mode === 'mfa' ? (
          <div className="space-y-1">
            <label htmlFor="otp" className="label">{t('Mã 6 số từ ứng dụng xác thực')}</label>
            <input
              id="otp"
              className="input text-center font-mono text-lg tracking-[0.4em]"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              required
              autoFocus
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
            />
          </div>
        ) : (
          <>
            <div className="space-y-1">
              <label htmlFor="email" className="label">{t('Email')}</label>
              <input id="email" type="email" required autoComplete="email" className="input" value={email} onChange={(e) => setEmail(e.target.value)} />
            </div>
            <div className="space-y-1">
              <label htmlFor="password" className="label">{t('Mật khẩu')}</label>
              <input
                id="password"
                type="password"
                required
                minLength={mode === 'signup' ? PASSWORD_MIN_LENGTH : 8}
                autoComplete={mode === 'signin' ? 'current-password' : 'new-password'}
                className="input"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
              {mode === 'signup' && (
                <p className={problems.length ? 'text-xs text-contested' : 'text-xs text-corroborated'}>
                  {problems.length ? t('Cần: {0}.', { 0: problems.join('; ') }) : password ? t('Mật khẩu đạt yêu cầu.') : t('Ít nhất {0} ký tự, kết hợp chữ hoa, chữ thường, số hoặc ký tự đặc biệt.', { 0: PASSWORD_MIN_LENGTH })}
                </p>
              )}
            </div>
          </>
        )}
        {error && <ErrorBox error={new Error(error)} />}
        {info && <Notice>{info}</Notice>}
        <button className="btn btn-primary w-full" disabled={busy || (mode === 'signup' && problems.length > 0)}>
          {busy ? <Spinner /> : mode === 'signin' ? <LogIn className="size-4" /> : mode === 'signup' ? <UserPlus className="size-4" /> : <KeyRound className="size-4" />}
          {mode === 'signin' ? t('Đăng nhập') : mode === 'signup' ? t('Tạo tài khoản') : t('Xác nhận')}
        </button>
        {mode === 'mfa' ? (
          <button
            type="button"
            className="w-full text-center text-sm text-accent hover:underline"
            onClick={async () => {
              await supabase?.auth.signOut({ scope: 'local' })
              setMode('signin')
              setCode('')
            }}
          >
            {t('Đăng nhập bằng tài khoản khác')}
          </button>
        ) : (
          <button
            type="button"
            className="w-full text-center text-sm text-accent hover:underline"
            onClick={() => setMode(mode === 'signin' ? 'signup' : 'signin')}
          >
            {mode === 'signin' ? t('Chưa có tài khoản? Tạo tài khoản') : t('Đã có tài khoản? Đăng nhập')}
          </button>
        )}
      </form>
    </Centered>
  )
}

