import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { LogOut, ShieldCheck, ShieldOff, ShieldPlus } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { api } from '../api'
import { exchangeToken, fetchSecurityEvents, supabase, useAuth } from '../auth/session'
import { ErrorBox, Loading, Notice, Pill, Spinner } from '../components/ui'
import { fmtDate } from '../lib/labels'
import { t, msg, translated } from '../i18n'

const EVENT_LABEL: Record<string, string> = translated({
  session_created: msg('Đăng nhập vào ứng dụng'),
  session_revoked: msg('Đăng xuất'),
  sessions_revoked_all: msg('Đăng xuất khỏi mọi thiết bị'),
  csrf_rejected: msg('Chặn yêu cầu giả mạo (CSRF)'),
  rate_limited: msg('Chặn vì quá nhiều yêu cầu'),
  mfa_required: msg('Chặn: thiếu xác thực hai lớp'),
  revoked_session_used: msg('Chặn: dùng lại phiên đã đăng xuất'),
})

/** Bảo mật tài khoản: xác thực hai lớp (TOTP), đăng xuất mọi thiết bị, nhật ký sự kiện bảo mật. */
export function SecurityPage() {
  if (api.mode === 'mock') {
    return (
      <div className="mx-auto max-w-2xl px-4 py-8">
        <Notice title={t('Chỉ có khi chạy với backend thật')}>{t('Chế độ mock không có tài khoản, xác thực hai lớp hay phiên đăng nhập.')}</Notice>
      </div>
    )
  }
  return (
    <div className="mx-auto max-w-2xl space-y-6 px-4 py-8">
      <div>
        <h1 className="text-2xl font-semibold">{t('Bảo mật tài khoản')}</h1>
        <p className="mt-1 text-muted">{t('Bật xác thực hai lớp, kiểm tra đăng nhập gần đây và thu hồi phiên trên mọi thiết bị.')}</p>
      </div>
      <MfaSection />
      <SessionsSection />
      <EventsSection />
    </div>
  )
}

function MfaSection() {
  const { user } = useAuth()
  const queryClient = useQueryClient()
  const factors = useQuery({
    queryKey: ['mfa-factors'],
    queryFn: async () => {
      const { data, error } = await supabase!.auth.mfa.listFactors()
      if (error) throw error
      return data.totp
    },
  })
  const [enroll, setEnroll] = useState<{ id: string; qr: string; secret: string } | null>(null)
  const [code, setCode] = useState('')
  const verified = factors.data?.find((f) => f.status === 'verified')

  const refresh = async () => {
    await queryClient.invalidateQueries({ queryKey: ['mfa-factors'] })
    await queryClient.invalidateQueries({ queryKey: ['auth', 'me'] })
  }

  const start = useMutation({
    mutationFn: async () => {
      // Dọn đăng ký dở trước đó (chưa xác minh) để không tích lũy factor rác.
      for (const factor of factors.data ?? []) {
        if (factor.status !== 'verified') await supabase!.auth.mfa.unenroll({ factorId: factor.id })
      }
      const { data, error } = await supabase!.auth.mfa.enroll({ factorType: 'totp', friendlyName: `MSKS ${new Date().toISOString().slice(0, 10)}` })
      if (error) throw error
      return { id: data.id, qr: data.totp.qr_code, secret: data.totp.secret }
    },
    onSuccess: setEnroll,
  })

  const confirm = useMutation({
    mutationFn: async () => {
      const { data, error } = await supabase!.auth.mfa.challengeAndVerify({ factorId: enroll!.id, code: code.trim() })
      if (error) throw new Error(t('Mã không đúng hoặc đã hết hạn. Kiểm tra đồng hồ điện thoại rồi thử lại.'))
      // Từ giờ server đòi phiên aal2: đổi ngay cookie sang token vừa qua 2FA.
      await exchangeToken(data.access_token)
    },
    onSuccess: async () => {
      setEnroll(null)
      setCode('')
      await refresh()
    },
  })

  const remove = useMutation({
    mutationFn: async () => {
      if (!confirmRemoval()) return
      const { error } = await supabase!.auth.mfa.unenroll({ factorId: verified!.id })
      if (error) throw error
      const { data } = await supabase!.auth.refreshSession()
      if (data.session) await exchangeToken(data.session.access_token)
    },
    onSuccess: refresh,
  })

  return (
    <section className="card space-y-3 p-5" aria-labelledby="mfa-heading">
      <div className="flex flex-wrap items-center gap-2">
        <h2 id="mfa-heading" className="font-semibold">{t('Xác thực hai lớp (TOTP)')}</h2>
        {verified ? (
          <Pill className="bg-corroborated-soft text-corroborated"><ShieldCheck className="size-3.5" /> {t('Đang bật')}</Pill>
        ) : (
          <Pill className="bg-contested-soft text-contested"><ShieldOff className="size-3.5" /> {t('Chưa bật')}</Pill>
        )}
        {user?.aal === 'aal2' && <span className="text-xs text-muted">{t('Phiên hiện tại đã qua xác thực hai lớp.')}</span>}
      </div>
      <p className="text-sm text-muted">
        {t('Khi bật, đăng nhập cần thêm mã 6 số từ ứng dụng xác thực (Google Authenticator, Microsoft Authenticator, 1Password…). Lộ mật khẩu không đủ để vào tài khoản; máy chủ từ chối mọi phiên chưa qua bước này.')}
      </p>
      {factors.isPending && <Loading />}
      {factors.error && <ErrorBox error={factors.error} onRetry={() => factors.refetch()} />}

      {!verified && !enroll && factors.data && (
        <button className="btn btn-primary" onClick={() => start.mutate()} disabled={start.isPending}>
          {start.isPending ? <Spinner /> : <ShieldPlus className="size-4" />} {t('Bật xác thực hai lớp')}
        </button>
      )}
      {enroll && (
        <form
          className="space-y-3 rounded-md border border-line p-4"
          onSubmit={(e: FormEvent) => {
            e.preventDefault()
            confirm.mutate()
          }}
        >
          <p className="text-sm">{t('1. Quét mã QR bằng ứng dụng xác thực:')}</p>
          <img src={enroll.qr} alt={t('Mã QR đăng ký ứng dụng xác thực')} className="size-44 rounded bg-white p-2" />
          <p className="text-xs text-muted">
            {t('Không quét được? Nhập khóa thủ công:')} <code className="select-all break-all font-mono">{enroll.secret}</code>{t('. Không chia sẻ mã này.')}
          </p>
          <label className="block text-sm">
            {t('2. Nhập mã 6 số đang hiển thị:')}
            <input
              className="input mt-1 w-40 text-center font-mono tracking-[0.3em]"
              inputMode="numeric"
              autoComplete="one-time-code"
              pattern="\d{6}"
              maxLength={6}
              required
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, ''))}
              aria-label={t('Mã xác thực 6 số')}
            />
          </label>
          <div className="flex gap-2">
            <button className="btn btn-primary" disabled={confirm.isPending || code.length !== 6}>
              {confirm.isPending && <Spinner />} {t('Xác nhận và bật')}
            </button>
            <button type="button" className="btn btn-ghost" onClick={() => setEnroll(null)}>
              {t('Hủy')}
            </button>
          </div>
          {confirm.error && <ErrorBox error={confirm.error} />}
        </form>
      )}
      {verified && (
        <button className="btn btn-outline text-rejected" onClick={() => remove.mutate()} disabled={remove.isPending || user?.aal !== 'aal2'}>
          {remove.isPending && <Spinner />} {t('Tắt xác thực hai lớp')}
        </button>
      )}
      {start.error && <ErrorBox error={start.error} />}
      {remove.error && <ErrorBox error={remove.error} />}
    </section>
  )
}

function confirmRemoval() {
  return window.confirm(t('Tắt xác thực hai lớp? Từ giờ chỉ cần mật khẩu là đăng nhập được.'))
}

function SessionsSection() {
  const { signOut } = useAuth()
  const everywhere = useMutation({
    mutationFn: () => signOut!({ scope: 'global', reason: t('Đã đăng xuất khỏi mọi thiết bị.') }),
  })
  return (
    <section className="card space-y-3 p-5" aria-labelledby="sessions-heading">
      <h2 id="sessions-heading" className="font-semibold">{t('Phiên đăng nhập')}</h2>
      <p className="text-sm text-muted">
        {t('Nghi ngờ lộ mật khẩu hoặc quên đăng xuất ở máy khác? Thu hồi mọi phiên: các thiết bị khác bị đăng xuất trong vòng 15 giây, kể cả khi token cũ chưa hết hạn. Nên đổi mật khẩu sau đó.')}
      </p>
      <button
        className="btn btn-outline text-rejected"
        disabled={everywhere.isPending}
        onClick={() => window.confirm(t('Đăng xuất khỏi mọi thiết bị, kể cả thiết bị này?')) && everywhere.mutate()}
      >
        {everywhere.isPending ? <Spinner /> : <LogOut className="size-4" />} {t('Đăng xuất khỏi mọi thiết bị')}
      </button>
      {everywhere.error && <ErrorBox error={everywhere.error} />}
    </section>
  )
}

function EventsSection() {
  const events = useQuery({ queryKey: ['security-events'], queryFn: fetchSecurityEvents })
  return (
    <section className="card space-y-3 p-5" aria-labelledby="events-heading">
      <h2 id="events-heading" className="font-semibold">{t('Hoạt động bảo mật gần đây')}</h2>
      {events.isPending && <Loading />}
      {events.error && <ErrorBox error={events.error} onRetry={() => events.refetch()} />}
      {events.data?.length === 0 && <p className="text-sm text-muted">{t('Chưa có sự kiện.')}</p>}
      <ul className="divide-y divide-line text-sm">
        {events.data?.map((e, i) => (
          <li key={`${e.created_at}-${i}`} className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 py-2">
            <span className={e.event.endsWith('_rejected') || e.event.startsWith('rate') || e.event.includes('required') || e.event.includes('used') ? 'text-contested' : ''}>
              {EVENT_LABEL[e.event] ?? e.event}
            </span>
            <span className="text-xs text-muted">{fmtDate(e.created_at)}</span>
            {e.ip && <span className="font-mono text-xs text-muted">{e.ip}</span>}
            {e.user_agent && (
              <span className="w-full truncate text-xs text-muted" title={e.user_agent}>
                {e.user_agent}
              </span>
            )}
          </li>
        ))}
      </ul>
      <p className="text-xs text-muted">{t('Thấy đăng nhập lạ? Đăng xuất khỏi mọi thiết bị, đổi mật khẩu và bật xác thực hai lớp.')}</p>
    </section>
  )
}
