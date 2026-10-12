import clsx from 'clsx'
import { Video } from 'lucide-react'
import type { ReactNode } from 'react'
import { BUDGETS, type Budget, type Source } from '../api'
import { useReadiness } from '../hooks/queries'
import { isLecture, isQueryable } from '../lib/labels'
import { t } from '../i18n'

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
  const ready = sources.filter((s) => isQueryable(s.status) && !s.deleted_at)
  const toggle = (id: string) => onChange(value.includes(id) ? value.filter((v) => v !== id) : [...value, id])
  const scope = value.length ? ready.filter((s) => value.includes(s.id)) : ready
  const mixed = scope.some((s) => isLecture(s.kind)) && scope.some((s) => !isLecture(s.kind))
  return (
    <fieldset>
      <legend className="label mb-1.5">{t('Phạm vi nguồn')}</legend>
      <div className="flex flex-wrap gap-1.5">
        <Chip active={value.length === 0} onClick={() => onChange([])}>
          {t('Tất cả nguồn sẵn sàng (')}{ready.length})
        </Chip>
        {ready.map((s) => (
          <Chip key={s.id} active={value.includes(s.id)} onClick={() => toggle(s.id)} title={s.title}>
            {isLecture(s.kind) && <Video className="size-3.5" aria-label={t('bài giảng')} />}
            <span className="font-semibold">{s.alias}</span>
            <span className="max-w-40 truncate">{s.title}</span>
          </Chip>
        ))}
      </div>
      <p className="mt-1.5 text-xs text-muted">{t('Lượt chạy chốt phiên bản nguồn lúc bắt đầu; nguồn nạp sau không chen vào lượt đang chạy.')}</p>
      {mixed && (
        <p className="mt-1 text-xs text-contested">
          {t('Phạm vi trộn bài giảng và tài liệu: hai loại dùng bộ mô hình khác nhau (tiếng Việt đa ngôn ngữ / tiếng Anh) nên chưa hỏi chung được. Hãy chọn riêng bài giảng hoặc riêng tài liệu.')}
        </p>
      )}
    </fieldset>
  )
}

export function BudgetSelect({ value, onChange }: { value: Budget; onChange: (budget: Budget) => void }) {
  return (
    <fieldset>
      <legend className="label mb-1.5">{t('Ngân sách context (token)')}</legend>
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
        ? t('Câu hỏi và các đoạn trích liên quan sẽ được gửi tới nhà cung cấp LLM: {0}.', { 0: provider })
        : t('Chế độ mock: không có dữ liệu nào được gửi ra ngoài trình duyệt.')}
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
