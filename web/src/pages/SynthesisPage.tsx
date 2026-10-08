import { useMutation } from '@tanstack/react-query'
import { ArrowDown, ArrowUp, FilePlus2, ListTree, Plus, Sparkles, Trash2, X } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { Navigate, useParams, useSearchParams } from 'react-router-dom'
import { api, newIdempotencyKey, type Budget, type OutlineSection } from '../api'
import { BudgetSelect, ProviderDisclosure, SourcePicker } from '../components/RunOptions'
import { RunView } from '../components/run/RunView'
import { ErrorBox, ExperimentalTag, Spinner } from '../components/ui'
import { useSources } from '../hooks/queries'
import { features } from '../lib/features'

const MAX_SECTIONS = 8
const MAX_QUESTIONS = 3

export function SynthesisPage() {
  const { workspaceId = '' } = useParams()
  const [params, setParams] = useSearchParams()
  const runId = params.get('run') ?? undefined

  if (!features.synthesis) return <Navigate to="../qa" replace />
  if (runId) {
    return (
      <div className="space-y-4">
        <button className="btn btn-outline" onClick={() => setParams({})}>
          <FilePlus2 className="size-4" /> Báo cáo mới
        </button>
        <RunView key={runId} runId={runId} />
      </div>
    )
  }
  return <SynthesisWizard workspaceId={workspaceId} onStarted={(id) => setParams({ run: id })} />
}

function SynthesisWizard({ workspaceId, onStarted }: { workspaceId: string; onStarted: (runId: string) => void }) {
  const sources = useSources(workspaceId)
  const [topic, setTopic] = useState('')
  const [scope, setScope] = useState<string[]>([])
  const [budget, setBudget] = useState<Budget>(2048)
  const [outline, setOutline] = useState<OutlineSection[] | null>(null)
  const keyRef = useRef<{ payload: string; key: string } | null>(null)

  const propose = useMutation({
    mutationFn: () => api.proposeOutline(workspaceId, { topic: topic.trim(), source_ids: scope }),
    onSuccess: setOutline,
  })
  const start = useMutation({
    mutationFn: (sections: OutlineSection[]) => {
      const request = { topic: topic.trim(), source_ids: scope, outline: sections, budget }
      const payload = JSON.stringify(request)
      if (keyRef.current?.payload !== payload) keyRef.current = { payload, key: newIdempotencyKey() }
      return api.startSynthesis(workspaceId, request, keyRef.current.key)
    },
    onSuccess: ({ run_id }) => onStarted(run_id),
  })

  function submitTopic(event: FormEvent) {
    event.preventDefault()
    if (topic.trim()) propose.mutate()
  }

  const cleaned = (outline ?? [])
    .map((s) => ({ ...s, title: s.title.trim(), questions: s.questions.map((q) => q.trim()).filter(Boolean) }))
    .filter((s) => s.title && s.questions.length)

  return (
    <div className="space-y-6">
      <div className="flex flex-wrap items-center gap-2">
        <h2 className="font-semibold">Báo cáo tổng hợp</h2>
        <ExperimentalTag />
      </div>
      <form onSubmit={submitTopic} className="card space-y-4 p-4">
        <div>
          <label htmlFor="topic" className="label mb-1.5 block">
            Bước 1 · Chủ đề tổng hợp
          </label>
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              id="topic"
              className="input"
              placeholder="Ví dụ: So sánh Agreement Ranking và RAPTOR cho QA trên bài báo dài"
              value={topic}
              onChange={(e) => setTopic(e.target.value)}
              maxLength={500}
            />
            <button className="btn btn-primary shrink-0" disabled={!topic.trim() || propose.isPending}>
              {propose.isPending ? <Spinner /> : <ListTree className="size-4" />} Đề xuất dàn ý
            </button>
          </div>
        </div>
        <div className="flex flex-col gap-4 border-t border-line pt-4 md:flex-row md:items-start md:justify-between">
          <SourcePicker sources={sources.data ?? []} value={scope} onChange={setScope} />
          <BudgetSelect value={budget} onChange={setBudget} />
        </div>
        <ProviderDisclosure />
      </form>
      {propose.error && <ErrorBox error={propose.error} onRetry={() => propose.mutate()} />}

      {outline && (
        <section className="card space-y-4 p-4">
          <div>
            <p className="label">Bước 2 · Chỉnh dàn ý</p>
            <p className="mt-1 text-sm text-muted">
              LLM chỉ đề xuất cần hỏi gì. Mỗi câu hỏi con chạy pipeline QA có kiểm chứng; báo cáo ghép claim đã kiểm chứng bằng template,
              không để LLM viết thêm câu sự kiện. Tối đa {MAX_SECTIONS} mục, mỗi mục {MAX_QUESTIONS} câu hỏi con, dùng chung một ngân sách.
            </p>
          </div>
          <OutlineEditor value={outline} onChange={setOutline} />
          <div className="flex flex-wrap items-center gap-3 border-t border-line pt-4">
            <button className="btn btn-primary" disabled={!cleaned.length || start.isPending} onClick={() => start.mutate(cleaned)}>
              {start.isPending ? <Spinner /> : <Sparkles className="size-4" />} Sinh báo cáo ({cleaned.length} mục)
            </button>
            {!cleaned.length && <span className="text-sm text-contested">Cần ít nhất một mục có tiêu đề và câu hỏi.</span>}
          </div>
          {start.error && <ErrorBox error={start.error} onRetry={() => start.mutate(cleaned)} />}
        </section>
      )}
    </div>
  )
}

function OutlineEditor({ value, onChange }: { value: OutlineSection[]; onChange: (outline: OutlineSection[]) => void }) {
  const update = (index: number, patch: Partial<OutlineSection>) =>
    onChange(value.map((s, i) => (i === index ? { ...s, ...patch } : s)))
  const move = (index: number, delta: number) => {
    const next = [...value]
    const [item] = next.splice(index, 1)
    next.splice(index + delta, 0, item)
    onChange(next)
  }

  return (
    <div className="space-y-3">
      {value.map((section, index) => (
        <div key={section.key} className="rounded-md border border-line bg-surface-2 p-3">
          <div className="flex items-center gap-2">
            <span className="w-6 shrink-0 text-center text-sm font-semibold text-muted">{index + 1}</span>
            <input
              className="input font-medium"
              value={section.title}
              placeholder="Tiêu đề mục"
              onChange={(e) => update(index, { title: e.target.value })}
              aria-label={`Tiêu đề mục ${index + 1}`}
            />
            <button type="button" className="btn btn-ghost p-2" disabled={index === 0} onClick={() => move(index, -1)} aria-label="Đưa lên">
              <ArrowUp className="size-4" />
            </button>
            <button type="button" className="btn btn-ghost p-2" disabled={index === value.length - 1} onClick={() => move(index, 1)} aria-label="Đưa xuống">
              <ArrowDown className="size-4" />
            </button>
            <button
              type="button"
              className="btn btn-ghost p-2 text-rejected"
              onClick={() => onChange(value.filter((_, i) => i !== index))}
              aria-label={`Xóa mục ${index + 1}`}
            >
              <Trash2 className="size-4" />
            </button>
          </div>
          <ul className="mt-2 space-y-1.5 pl-8">
            {section.questions.map((question, qi) => (
              <li key={qi} className="flex items-center gap-2">
                <input
                  className="input py-1.5"
                  value={question}
                  placeholder="Câu hỏi con"
                  aria-label={`Câu hỏi con ${qi + 1} của mục ${index + 1}`}
                  onChange={(e) => update(index, { questions: section.questions.map((q, j) => (j === qi ? e.target.value : q)) })}
                />
                <button
                  type="button"
                  className="btn btn-ghost p-1.5"
                  onClick={() => update(index, { questions: section.questions.filter((_, j) => j !== qi) })}
                  aria-label="Xóa câu hỏi con"
                >
                  <X className="size-4" />
                </button>
              </li>
            ))}
          </ul>
          {section.questions.length < MAX_QUESTIONS && (
            <button
              type="button"
              className="btn btn-ghost ml-8 mt-1 px-2 py-1 text-xs text-accent"
              onClick={() => update(index, { questions: [...section.questions, ''] })}
            >
              <Plus className="size-3.5" /> Thêm câu hỏi con
            </button>
          )}
        </div>
      ))}
      {value.length < MAX_SECTIONS && (
        <button
          type="button"
          className="btn btn-outline"
          onClick={() => onChange([...value, { key: `custom_${crypto.randomUUID().slice(0, 8)}`, title: '', questions: [''] }])}
        >
          <Plus className="size-4" /> Thêm mục
        </button>
      )}
    </div>
  )
}
