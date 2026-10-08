import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Ban, CircleSlash, RotateCcw, WifiOff } from 'lucide-react'
import { useRef } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { api, newIdempotencyKey, type ClaimResult, type ReportSection, type Run } from '../../api'
import { useRunStream } from '../../hooks/useRunStream'
import { ANSWER_STATUS, QUERY_TYPE, fmtDate, isTerminal } from '../../lib/labels'
import { AnswerStatusBadge, RunStatusBadge } from '../badges'
import { ClaimCard } from '../ClaimCard'
import { ErrorBox, Loading, Notice, Pill, Spinner } from '../ui'
import { AuditPanel } from './AuditPanel'
import { ConsensusTable } from './ConsensusTable'
import { ExportMenu } from './ExportMenu'
import { MetricsPanel } from './MetricsPanel'
import { StageProgress } from './StageProgress'

/** Một lượt chạy: nhận SSE khi đang chạy, đọc kết quả đã persist khi đã kết thúc. */
export function RunView({ runId }: { runId: string }) {
  const runQuery = useQuery({
    queryKey: ['run', runId],
    queryFn: () => api.getRun(runId),
    // SSE can disconnect after the final commit. Polling recovers terminal state.
    refetchInterval: (query) => query.state.data && !isTerminal(query.state.data.status) ? 5000 : false,
  })
  const live = !!runQuery.data && !isTerminal(runQuery.data.status)
  const stream = useRunStream(live ? runId : undefined)

  if (runQuery.isPending) return <Loading />
  if (runQuery.error) return <ErrorBox error={runQuery.error} onRetry={() => runQuery.refetch()} />

  const run = runQuery.data
  const terminal = isTerminal(run.status)
  // Khi đang chạy: claim/section từ stream (đã dedup theo seq) phủ lên snapshot ban đầu.
  const claims = terminal ? run.claims : mergeById(run.claims, stream.claims)
  const sections = terminal ? (run.sections ?? []) : mergeSections(run.sections ?? [], stream.sections)
  const waitingForFinal = !terminal && !!stream.done

  return (
    <div className="grid gap-6 lg:grid-cols-[minmax(0,1fr)_300px]">
      <div className="min-w-0 space-y-5">
        <RunHeader run={run} />

        {run.provenance && !run.provenance.calibration_artifact && (
          <Notice tone="warn" title="Kết quả thử nghiệm">
            Ngưỡng kiểm chứng chưa có calibration artifact cho model/miền này. "Được bằng chứng hỗ trợ" là tín hiệu của verifier,
            không phải bảo đảm sự thật.
          </Notice>
        )}
        {stream.connection === 'reconnecting' && (
          <Notice title="Mất kết nối, đang kết nối lại…">
            <span className="inline-flex items-center gap-1">
              <WifiOff className="size-3.5" /> Lượt chạy vẫn tiếp tục trên máy chủ; các sự kiện sẽ được phát lại.
            </span>
          </Notice>
        )}
        {stream.error && <ErrorBox error={new Error(stream.error.message)} />}
        <TerminalNotice run={run} />

        {run.mode === 'qa' ? (
          <ClaimList claims={claims} running={!terminal} />
        ) : (
          <Report sections={sections} claims={claims} running={!terminal} />
        )}

        {terminal && run.rejected_claims.length > 0 && (
          <p className="text-sm text-muted">
            {run.rejected_claims.length} claim không qua kiểm chứng nên không hiển thị ở trên — xem tab “Claim bị loại”.
          </p>
        )}
        {terminal && <ConsensusTable claims={run.claims} />}
        {terminal && <AuditPanel claims={run.claims} rejected={run.rejected_claims} context={run.context} provenance={run.provenance} />}
      </div>

      <aside className="space-y-4">
        <section className="card p-4">
          <h3 className="label mb-3">Tiến trình</h3>
          {terminal ? (
            <RunStatusBadge status={run.status} />
          ) : (
            <StageProgress stages={stream.stages} running={!waitingForFinal} />
          )}
          {!terminal && <CancelButton run={run} />}
        </section>
        {terminal && run.metrics && (
          <section className="card p-4">
            <h3 className="label mb-1">Chỉ số kiểm chứng</h3>
            <MetricsPanel metrics={run.metrics} />
          </section>
        )}
        {(run.status === 'succeeded' || run.status === 'partial') && (
          <section className="card p-4">
            <h3 className="label mb-3">Xuất kết quả</h3>
            <ExportMenu runId={run.run_id} />
          </section>
        )}
        {terminal && run.mode === 'qa' && <RerunButton run={run} />}
      </aside>
    </div>
  )
}

function RunHeader({ run }: { run: Run }) {
  return (
    <header className="space-y-2">
      <div className="flex flex-wrap items-center gap-2 text-xs text-muted">
        <Pill className="bg-surface-2 text-fg">{run.mode === 'qa' ? 'Hỏi đáp' : 'Tổng hợp'}</Pill>
        {run.query_type && <Pill className="bg-accent-soft text-accent">{QUERY_TYPE[run.query_type]}</Pill>}
        <RunStatusBadge status={run.status} />
        {run.answer_status && <AnswerStatusBadge status={run.answer_status} />}
        <span>Ngân sách {run.budget.context_tokens} token</span>
        <span>·</span>
        <span>{fmtDate(run.created_at)}</span>
        {run.rerun_of && (
          <>
            <span>·</span>
            <Link to={`/w/${run.workspace_id}/runs/${run.rerun_of}`} className="text-accent hover:underline">
              chạy lại từ lượt trước
            </Link>
          </>
        )}
      </div>
      <h2 className="text-xl font-semibold leading-snug">{run.query}</h2>
    </header>
  )
}

function TerminalNotice({ run }: { run: Run }) {
  if (run.status === 'failed') {
    return <ErrorBox error={new Error(run.error?.message ?? 'Lượt chạy thất bại.')} />
  }
  if (run.status === 'cancelled') {
    return (
      <Notice title="Lượt chạy đã bị hủy">
        Các claim đã kiểm chứng trước khi hủy vẫn được giữ; kết quả trả về sau khi hủy không được publish.
      </Notice>
    )
  }
  if (run.status === 'partial') {
    return (
      <Notice tone="warn" title="Hoàn tất một phần">
        {run.error?.message ?? 'Một số phần không hoàn tất.'}
      </Notice>
    )
  }
  if (run.status === 'succeeded' && run.answer_status && run.answer_status !== 'answered') {
    return (
      <div className="card flex items-start gap-3 p-4">
        <CircleSlash className="mt-0.5 size-5 shrink-0 text-muted" />
        <div>
          <p className="font-medium">{ANSWER_STATUS[run.answer_status].label}</p>
          <p className="mt-1 text-sm text-muted">{run.error?.message ?? ANSWER_STATUS[run.answer_status].hint}</p>
        </div>
      </div>
    )
  }
  return null
}

function CancelButton({ run }: { run: Run }) {
  const queryClient = useQueryClient()
  const cancel = useMutation({
    mutationFn: () => api.cancelRun(run.run_id),
    onSettled: () => queryClient.invalidateQueries({ queryKey: ['run', run.run_id] }),
  })
  const requested = run.cancel_requested || cancel.isSuccess
  return (
    <div className="mt-3 space-y-2">
      <button className="btn btn-outline w-full" onClick={() => cancel.mutate()} disabled={requested || cancel.isPending}>
        {cancel.isPending ? <Spinner /> : <Ban className="size-4" />}
        {requested ? 'Đã yêu cầu hủy' : 'Hủy lượt chạy'}
      </button>
      {cancel.error && <ErrorBox error={cancel.error} />}
    </div>
  )
}

/** Rerun: lượt mới tham chiếu lượt cũ, có thể khác kết quả do model/provider (mục 15). */
function RerunButton({ run }: { run: Run }) {
  const navigate = useNavigate()
  const key = useRef(newIdempotencyKey())
  const rerun = useMutation({
    mutationFn: () =>
      api.askQuestion(
        run.workspace_id,
        { question: run.query,
          source_ids: [...new Set(run.provenance?.source_snapshot.map((source) => source.source_id) ?? run.source_ids)],
          budget: run.budget.context_tokens, mode: 'standard', rerun_of: run.run_id },
        key.current,
      ),
    onSuccess: ({ run_id }) => navigate(`/w/${run.workspace_id}/qa?run=${run_id}`),
  })
  return (
    <section className="card space-y-2 p-4">
      <button className="btn btn-outline w-full" onClick={() => rerun.mutate()} disabled={rerun.isPending}>
        {rerun.isPending ? <Spinner /> : <RotateCcw className="size-4" />} Chạy lại câu hỏi
      </button>
      <p className="text-xs text-muted">Dùng cùng tập nguồn, với phiên bản nguồn và cấu hình máy chủ hiện tại. Kết quả có thể khác.</p>
      {rerun.error && <ErrorBox error={rerun.error} />}
    </section>
  )
}

function VerifyingPlaceholder() {
  return (
    <div className="card flex items-center gap-3 p-4 text-sm text-muted">
      <Spinner /> Claim chỉ xuất hiện ở đây sau khi đã qua kiểm chứng…
    </div>
  )
}

function ClaimList({ claims, running }: { claims: ClaimResult[]; running: boolean }) {
  return (
    <div className="space-y-3">
      {claims.map((claim) => (
        <ClaimCard key={claim.id} claim={claim} />
      ))}
      {running && <VerifyingPlaceholder />}
    </div>
  )
}

const SECTION_NOTE: Record<ReportSection['status'], string | null> = {
  complete: null,
  insufficient_evidence: 'Không đủ bằng chứng',
  failed: 'Không hoàn tất',
}

function Report({ sections, claims, running }: { sections: ReportSection[]; claims: ClaimResult[]; running: boolean }) {
  const byId = new Map(claims.map((c) => [c.id, c]))
  const placed = new Set(sections.flatMap((s) => s.paragraphs.flatMap((p) => p.claim_ids)))
  const inProgress = claims.filter((c) => !placed.has(c.id))
  return (
    <div className="space-y-8">
      {sections.map((section) => (
        <section key={section.key} className="space-y-3">
          <h3 className="flex flex-wrap items-center gap-2 border-b border-line pb-1 text-lg font-semibold">
            {section.title}
            {SECTION_NOTE[section.status] && (
              <Pill className={section.status === 'failed' ? 'bg-rejected-soft text-rejected' : 'bg-surface-2 text-muted'}>
                {SECTION_NOTE[section.status]}
              </Pill>
            )}
          </h3>
          {section.paragraphs.map((p, i) => (
            <div key={i} className="space-y-3">
              <p className="text-muted">{p.template_text}</p>
              {p.claim_ids.map((id) => {
                const claim = byId.get(id)
                return claim ? <ClaimCard key={id} claim={claim} /> : null
              })}
            </div>
          ))}
        </section>
      ))}
      {running && (
        <section className="space-y-3">
          <h3 className="label">Đang soạn mục tiếp theo…</h3>
          {inProgress.map((claim) => (
            <ClaimCard key={claim.id} claim={claim} />
          ))}
          <VerifyingPlaceholder />
        </section>
      )}
    </div>
  )
}

function mergeById(base: ClaimResult[], updates: ClaimResult[]) {
  const map = new Map(base.map((c) => [c.id, c]))
  for (const claim of updates) map.set(claim.id, claim)
  return [...map.values()]
}

function mergeSections(base: ReportSection[], updates: ReportSection[]) {
  const map = new Map(base.map((s) => [s.key, s]))
  for (const section of updates) map.set(section.key, section)
  return [...map.values()]
}
