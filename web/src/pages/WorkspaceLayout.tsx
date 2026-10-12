import clsx from 'clsx'
import { ChevronLeft } from 'lucide-react'
import { NavLink, Outlet, useParams } from 'react-router-dom'
import { ApiError } from '../api'
import { EmptyState, ErrorBox } from '../components/ui'
import { ViewerProvider } from '../components/viewer'
import { useSources, useWorkspace } from '../hooks/queries'
import { features } from '../lib/features'
import { t, msg, translated } from '../i18n'

const TABS = translated([
  { to: 'sources', label: msg('Nguồn') },
  { to: 'qa', label: msg('Hỏi đáp') },
  ...(features.synthesis ? [{ to: 'synthesis', label: msg('Tổng hợp') }] : []),
  { to: 'runs', label: msg('Lịch sử') },
])

export function WorkspaceLayout() {
  const { workspaceId = '' } = useParams()
  const workspace = useWorkspace(workspaceId)
  const sources = useSources(workspaceId)

  if (workspace.error instanceof ApiError && [403, 404].includes(workspace.error.status)) {
    return (
      <div className="mx-auto max-w-xl px-4 py-16">
        <EmptyState icon={null} title={t('Không tìm thấy workspace')}>
          {t('Workspace không tồn tại hoặc bạn không có quyền truy cập.')}{' '}
          <NavLink to="/" className="text-accent hover:underline">
            {t('Về danh sách workspace')}
          </NavLink>
        </EmptyState>
      </div>
    )
  }

  return (
    <ViewerProvider sources={sources.data ?? []}>
      <div className="border-b border-line bg-surface">
        <div className="mx-auto max-w-6xl px-4 pt-4">
          <NavLink to="/" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
            <ChevronLeft className="size-4" /> {t('Workspace')}
          </NavLink>
          <h1 className="mt-1 min-h-7 truncate text-xl font-semibold">{workspace.data?.name}</h1>
          <nav className="-mb-px mt-3 flex gap-1 overflow-x-auto" aria-label={t('Mục trong workspace')}>
            {TABS.map((tab) => (
              <NavLink
                key={tab.to}
                to={tab.to}
                className={({ isActive }) =>
                  clsx(
                    'whitespace-nowrap border-b-2 px-3 py-2 text-sm',
                    isActive ? 'border-accent font-medium text-fg' : 'border-transparent text-muted hover:text-fg',
                  )
                }
              >
                {tab.label}
              </NavLink>
            ))}
          </nav>
        </div>
      </div>
      <main className="mx-auto max-w-6xl px-4 py-6">
        {workspace.error ? <ErrorBox error={workspace.error} onRetry={() => workspace.refetch()} /> : <Outlet />}
      </main>
    </ViewerProvider>
  )
}
