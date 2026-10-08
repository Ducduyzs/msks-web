import { History } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { AnswerStatusBadge, RunStatusBadge } from '../components/badges'
import { EmptyState, ErrorBox, Loading, Pill, Spinner } from '../components/ui'
import { useRuns } from '../hooks/queries'
import { fmtDate } from '../lib/labels'

export function RunsPage() {
  const { workspaceId = '' } = useParams()
  const runs = useRuns(workspaceId)

  if (runs.isPending) return <Loading />
  if (runs.error) return <ErrorBox error={runs.error} onRetry={() => runs.refetch()} />
  const items = runs.data.pages.flatMap((p) => p.items)
  if (!items.length) {
    return (
      <EmptyState icon={<History className="size-8" />} title="Chưa có lượt chạy nào">
        Kết quả hỏi đáp và báo cáo tổng hợp được lưu ở đây, kèm cấu hình và phiên bản nguồn đã dùng.
      </EmptyState>
    )
  }
  return (
    <div className="space-y-3">
      <ul className="space-y-2">
        {items.map((run) => (
          <li key={run.run_id}>
            <Link to={run.run_id} className="card flex flex-col gap-2 p-4 hover:shadow-md sm:flex-row sm:items-center">
              <div className="flex shrink-0 flex-wrap gap-1.5">
                <Pill className="bg-surface-2 text-fg">{run.mode === 'qa' ? 'Hỏi đáp' : 'Tổng hợp'}</Pill>
                <RunStatusBadge status={run.status} />
                {run.answer_status && <AnswerStatusBadge status={run.answer_status} />}
              </div>
              <p className="min-w-0 flex-1 truncate font-medium">{run.query}</p>
              <span className="flex shrink-0 flex-wrap items-center gap-x-2 text-sm text-muted">
                <span>{run.accepted_count} claim</span>
                <span>·</span>
                <span>{run.budget.context_tokens} token</span>
                <span>·</span>
                <span>{fmtDate(run.created_at)}</span>
                {run.rerun_of && <span>· chạy lại</span>}
              </span>
            </Link>
          </li>
        ))}
      </ul>
      {runs.hasNextPage && (
        <button className="btn btn-outline" onClick={() => runs.fetchNextPage()} disabled={runs.isFetchingNextPage}>
          {runs.isFetchingNextPage && <Spinner />} Tải thêm
        </button>
      )}
    </div>
  )
}
