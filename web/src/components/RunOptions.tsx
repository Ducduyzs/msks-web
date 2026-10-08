import clsx from 'clsx'
import type { ReactNode } from 'react'
import { BUDGETS, type Budget, type Source } from '../api'
import { useReadiness } from '../hooks/queries'

/** Chọn phạm vi nguồn; danh sách rỗng nghĩa là mọi nguồn sẵn sàng tại thời điểm chạy. */
export function SourcePicker({
  sources,
  value,
  onChange,
}: {
  sources: Source[]
  value: string[]
  onChange: (ids: string[]) => void
}) {
  const ready = sources.filter((s) => s.status === 'ready' && !s.deleted_at)
  const toggle = (id: string) => onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id])
  return (
    <fieldset>
      <legend className="label mb-1.5">Phạm vi nguồn</legend>
      <div className="flex flex-wrap gap-1.5">
        <Chip active={value.length === 0} onClick={() => onChange([])}>
          Tất cả nguồn sẵn sàng ({ready.length})
        </Chip>
        {ready.map((s) => (
          <Chip key={s.id} active={value.includes(s.id)} onClick={() => toggle(s.id)} title={s.title}>
            <span className="font-semibold">{s.alias}</span>
            <span className="max-w-40 truncate">{s.title}</span>
          </Chip>
        ))}
      </div>
      <p className="mt-1.5 text-xs text-muted">Lượt chạy chốt phiên bản nguồn lúc bắt đầu; nguồn nạp sau không chen vào lượt đang chạy.</p>
    </fieldset>
  )
}

export function BudgetSelect({ value, onChange }: { value: Budget; onChange: (budget: Budget) => void }) {
  return (
    <fieldset>
      <legend className="label mb-1.5">Ngân sách context (token)</legend>
      <div className="inline-flex rounded-md border border-line bg-surface p-0.5" role="radiogroup">
        {BUDGETS.map((b) => (
          <button
            key={b}
            type="button"
            role="radio"
            aria-checked={b === value}
            onClick={() => onChange(b)}
            className={clsx('rounded px-3 py-1 text-sm tabular-nums', b === value ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg')}
          >
            {b}
          </button>
        ))}
      </div>
    </fieldset>
  )
}

/** Báo rõ khi đoạn trích nguồn được gửi tới nhà cung cấp LLM bên ngoài (mục 15). */
export function ProviderDisclosure() {
  const readiness = useReadiness()
  const provider = readiness.data?.llm_provider
  if (readiness.isPending) return null
  return (
    <p className="text-xs text-muted">
      {provider
        ? `Câu hỏi và các đoạn trích liên quan sẽ được gửi tới nhà cung cấp LLM: ${provider}.`
        : 'Chế độ mock: không có dữ liệu nào được gửi ra ngoài trình duyệt.'}
    </p>
  )
}

function Chip({ active, onClick, title, children }: { active: boolean; onClick: () => void; title?: string; children: ReactNode }) {
  return (
    <button
      type="button"
      title={title}
      onClick={onClick}
      aria-pressed={active}
      className={clsx(
        'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs',
        active ? 'border-accent bg-accent-soft text-accent' : 'border-line bg-surface text-muted hover:text-fg',
      )}
    >
      {children}
    </button>
  )
}
