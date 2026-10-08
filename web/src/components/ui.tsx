import clsx from 'clsx'
import { AlertTriangle, Info, Loader2 } from 'lucide-react'
import type { ReactNode } from 'react'
import { ApiError } from '../api'

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx('size-4 animate-spin', className)} aria-hidden />
}

export function Loading({ label = 'Đang tải…' }: { label?: string }) {
  return (
    <div className="flex items-center gap-2 py-8 text-sm text-muted" role="status">
      <Spinner /> {label}
    </div>
  )
}

/** Hiển thị lỗi; với ApiError kèm mã lỗi và trace_id để đối chiếu log. */
export function ErrorBox({ error, onRetry }: { error: unknown; onRetry?: () => void }) {
  const message = error instanceof Error ? error.message : String(error)
  const api = error instanceof ApiError ? error : null
  return (
    <div role="alert" className="flex items-start gap-2 rounded-md border border-rejected/40 bg-rejected-soft px-3 py-2 text-sm text-rejected">
      <AlertTriangle className="mt-0.5 size-4 shrink-0" aria-hidden />
      <div className="min-w-0 flex-1">
        <p>{message}</p>
        {api && (
          <p className="mt-0.5 font-mono text-xs opacity-80">
            {api.code}
            {api.traceId && ` · trace ${api.traceId}`}
          </p>
        )}
      </div>
      {onRetry && (!api || api.retryable) && (
        <button className="shrink-0 text-xs font-medium underline" onClick={onRetry}>
          Thử lại
        </button>
      )}
    </div>
  )
}

export function Notice({ tone = 'info', title, children }: { tone?: 'info' | 'warn'; title?: string; children: ReactNode }) {
  return (
    <div
      className={clsx(
        'flex items-start gap-2 rounded-md border px-3 py-2 text-sm',
        tone === 'warn' ? 'border-contested/40 bg-contested-soft text-fg' : 'border-line bg-surface-2 text-fg',
      )}
    >
      {tone === 'warn' ? (
        <AlertTriangle className="mt-0.5 size-4 shrink-0 text-contested" aria-hidden />
      ) : (
        <Info className="mt-0.5 size-4 shrink-0 text-muted" aria-hidden />
      )}
      <div>
        {title && <p className="font-medium">{title}</p>}
        <div className={title ? 'mt-0.5 text-muted' : ''}>{children}</div>
      </div>
    </div>
  )
}

export function EmptyState({ icon, title, children }: { icon: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="card flex flex-col items-center gap-2 px-6 py-12 text-center">
      <div className="text-muted">{icon}</div>
      <p className="font-medium">{title}</p>
      {children && <div className="max-w-md text-sm text-muted">{children}</div>}
    </div>
  )
}

export function ProgressBar({ value, label }: { value: number; label: string }) {
  return (
    <div
      className="h-1.5 w-full overflow-hidden rounded-full bg-surface-2"
      role="progressbar"
      aria-label={label}
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(value * 100)}
    >
      <div className="h-full rounded-full bg-accent transition-[width] duration-500" style={{ width: `${value * 100}%` }} />
    </div>
  )
}

export function Pill({ children, className, title }: { children: ReactNode; className?: string; title?: string }) {
  return (
    <span title={title} className={clsx('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium', className)}>
      {children}
    </span>
  )
}

export function ExperimentalTag() {
  return (
    <Pill className="bg-contested-soft text-contested" title="Tính năng sau MVP, đang ở mức thử nghiệm">
      Thử nghiệm
    </Pill>
  )
}
