import clsx from 'clsx'
import { Layers, LogOut, ShieldCheck } from 'lucide-react'
import { Link, Outlet, isRouteErrorResponse, useRouteError } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth/session'
import { useReadiness } from '../hooks/queries'
import { PreferencesMenu } from './PreferencesMenu'
import { Pill } from './ui'
import { t } from '../i18n'

function ReadinessDot() {
  const readiness = useReadiness()
  if (readiness.isPending) return null
  const down = readiness.error || !readiness.data?.ready
  const degraded = !down && Object.values(readiness.data?.components ?? {}).some((s) => s !== 'ok')
  const detail = readiness.data
    ? Object.entries(readiness.data.components)
        .map(([k, v]) => `${k}: ${v}`)
        .join('\n')
    : t('Không kiểm tra được trạng thái máy chủ')
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted" title={detail}>
      <span className={clsx('size-2 rounded-full', down ? 'bg-rejected' : degraded ? 'bg-contested' : 'bg-corroborated')} />
      <span className="hidden sm:inline">{down ? t('Máy chủ chưa sẵn sàng') : degraded ? t('Một số dịch vụ chậm') : t('Sẵn sàng')}</span>
    </span>
  )
}

function UserMenu() {
  const { user, signOut } = useAuth()
  if (!user || !signOut) return null
  return (
    <span className="flex items-center gap-2 text-sm">
      <span className="hidden max-w-48 truncate text-muted sm:inline" title={user.email ?? user.id}>
        {user.email ?? user.id}
      </span>
      <Link to="/security" className="btn btn-ghost px-2 py-1" aria-label={t('Bảo mật tài khoản')} title={t('Bảo mật tài khoản')}>
        <ShieldCheck className={clsx('size-4', user.mfa_enabled ? 'text-corroborated' : 'text-contested')} />
      </Link>
      <button className="btn btn-ghost px-2 py-1" onClick={() => signOut()} aria-label={t('Đăng xuất')} title={t('Đăng xuất')}>
        <LogOut className="size-4" />
      </button>
    </span>
  )
}

export function Shell() {
  return (
    <div className="flex min-h-full flex-col">
      <a href="#content" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
        {t('Bỏ qua tới nội dung')}
      </a>
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
          <Link to="/" className="flex items-center gap-2 font-semibold">
            <Layers className="size-5 text-accent" />
            MSKS
          </Link>
          <span className="hidden text-sm text-muted md:inline">{t('Tổng hợp kiến thức đa nguồn có kiểm chứng')}</span>
          <div className="ml-auto flex items-center gap-3">
            <ReadinessDot />
            <PreferencesMenu />
            <UserMenu />
            {api.mode === 'mock' && (
              <Pill className="bg-contested-soft text-contested" title={t('API giả lập trong trình duyệt (VITE_API_MODE=mock)')}>
                {t('Dữ liệu mẫu')}
              </Pill>
            )}
          </div>
        </div>
      </header>
      <div id="content" className="flex-1">
        <Outlet />
      </div>
    </div>
  )
}

export function NotFound() {
  return (
    <div className="mx-auto max-w-xl px-4 py-16 text-center">
      <p className="text-lg font-semibold">{t('Không tìm thấy trang')}</p>
      <Link to="/" className="mt-2 inline-block text-accent hover:underline">
        {t('Về danh sách workspace')}
      </Link>
    </div>
  )
}

/** Lỗi render không mong đợi: hiện thông báo thay vì trang trắng. */
export function RouteError() {
  const error = useRouteError()
  const message = isRouteErrorResponse(error) ? `${error.status} ${error.statusText}` : error instanceof Error ? error.message : String(error)
  return (
    <div className="mx-auto max-w-xl px-4 py-16 text-center">
      <p className="text-lg font-semibold">{t('Đã có lỗi xảy ra')}</p>
      <p className="mt-2 font-mono text-sm text-muted">{message}</p>
      <Link to="/" className="mt-4 inline-block text-accent hover:underline">
        {t('Về trang chính')}
      </Link>
    </div>
  )
}
