import clsx from 'clsx'
import { Layers, LogOut } from 'lucide-react'
import { Link, Outlet, isRouteErrorResponse, useRouteError } from 'react-router-dom'
import { api } from '../api'
import { useAuth } from '../auth/session'
import { useReadiness } from '../hooks/queries'
import { Pill } from './ui'

function ReadinessDot() {
  const readiness = useReadiness()
  if (readiness.isPending) return null
  const down = readiness.error || !readiness.data?.ready
  const degraded = !down && Object.values(readiness.data?.components ?? {}).some((s) => s !== 'ok')
  const detail = readiness.data
    ? Object.entries(readiness.data.components)
        .map(([k, v]) => `${k}: ${v}`)
        .join('\n')
    : 'Không kiểm tra được trạng thái máy chủ'
  return (
    <span className="flex items-center gap-1.5 text-xs text-muted" title={detail}>
      <span className={clsx('size-2 rounded-full', down ? 'bg-rejected' : degraded ? 'bg-contested' : 'bg-corroborated')} />
      <span className="hidden sm:inline">{down ? 'Máy chủ chưa sẵn sàng' : degraded ? 'Một số dịch vụ chậm' : 'Sẵn sàng'}</span>
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
      <button className="btn btn-ghost px-2 py-1" onClick={() => signOut()} aria-label="Đăng xuất" title="Đăng xuất">
        <LogOut className="size-4" />
      </button>
    </span>
  )
}

export function Shell() {
  return (
    <div className="flex min-h-full flex-col">
      <a href="#content" className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-surface focus:px-3 focus:py-2">
        Bỏ qua tới nội dung
      </a>
      <header className="border-b border-line bg-surface">
        <div className="mx-auto flex h-14 max-w-6xl items-center gap-3 px-4">
          <Link to="/" className="flex items-center gap-2 font-semibold">
            <Layers className="size-5 text-accent" />
            MSKS
          </Link>
          <span className="hidden text-sm text-muted md:inline">Tổng hợp kiến thức đa nguồn có kiểm chứng</span>
          <div className="ml-auto flex items-center gap-3">
            <ReadinessDot />
            <UserMenu />
            {api.mode === 'mock' && (
              <Pill className="bg-contested-soft text-contested" title="API giả lập trong trình duyệt (VITE_API_MODE=mock)">
                Dữ liệu mẫu
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
      <p className="text-lg font-semibold">Không tìm thấy trang</p>
      <Link to="/" className="mt-2 inline-block text-accent hover:underline">
        Về danh sách workspace
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
      <p className="text-lg font-semibold">Đã có lỗi xảy ra</p>
      <p className="mt-2 font-mono text-sm text-muted">{message}</p>
      <Link to="/" className="mt-4 inline-block text-accent hover:underline">
        Về trang chính
      </Link>
    </div>
  )
}
