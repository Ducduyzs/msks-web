import { useMutation } from '@tanstack/react-query'
import { Send } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { api, newIdempotencyKey, type Budget } from '../api'
import { BudgetSelect, ProviderDisclosure, SourcePicker } from '../components/RunOptions'
import { RunView } from '../components/run/RunView'
import { ErrorBox, Spinner } from '../components/ui'
import { useSources } from '../hooks/queries'

const EXAMPLES = [
  'Agreement Ranking so với RAPTOR thế nào trên tài liệu dài?',
  'RAPTOR xây dựng chỉ mục như thế nào?',
  'Chi phí dựng index của hai phương pháp ra sao?',
]

export function QaPage() {
  const { workspaceId = '' } = useParams()
  const sources = useSources(workspaceId)
  const [params, setParams] = useSearchParams()
  const runId = params.get('run') ?? undefined
  const [question, setQuestion] = useState('')
  const [scope, setScope] = useState<string[]>([])
  const [budget, setBudget] = useState<Budget>(2048)
  // Một khóa cho mỗi lần gửi; thử lại cùng nội dung dùng lại khóa để server không tạo run trùng.
  const keyRef = useRef<{ payload: string; key: string } | null>(null)

  const ask = useMutation({
    mutationFn: () => {
      const request = { question: question.trim(), source_ids: scope, budget, mode: 'standard' as const }
      const payload = JSON.stringify(request)
      if (keyRef.current?.payload !== payload) keyRef.current = { payload, key: newIdempotencyKey() }
      return api.askQuestion(workspaceId, request, keyRef.current.key)
    },
    onSuccess: ({ run_id }) => {
      keyRef.current = null
      setParams({ run: run_id })
    },
  })

  const readyCount = sources.data?.filter((s) => s.status === 'ready' && !s.deleted_at).length ?? 0
  const canSubmit = question.trim().length >= 3 && readyCount > 0 && !ask.isPending

  function submit(event: Pick<FormEvent, 'preventDefault'>) {
    event.preventDefault()
    if (canSubmit) ask.mutate()
  }

  return (
    <div className="space-y-6">
      <form onSubmit={submit} className="card space-y-4 p-4">
        <label htmlFor="question" className="sr-only">
          Câu hỏi
        </label>
        <div className="flex flex-col gap-2 sm:flex-row">
          <textarea
            id="question"
            className="input min-h-[52px] resize-y"
            rows={2}
            placeholder="Đặt câu hỏi về các nguồn trong workspace… (Enter để gửi, Shift+Enter xuống dòng)"
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) submit(e)
            }}
            maxLength={4000}
          />
          <button className="btn btn-primary shrink-0 sm:self-start" disabled={!canSubmit}>
            {ask.isPending ? <Spinner /> : <Send className="size-4" />} Hỏi
          </button>
        </div>
        <div className="flex flex-wrap gap-x-3 gap-y-1">
          {EXAMPLES.map((example) => (
            <button key={example} type="button" className="text-xs text-accent hover:underline" onClick={() => setQuestion(example)}>
              {example}
            </button>
          ))}
        </div>
        <div className="flex flex-col gap-4 border-t border-line pt-4 md:flex-row md:items-start md:justify-between">
          <SourcePicker sources={sources.data ?? []} value={scope} onChange={setScope} />
          <BudgetSelect value={budget} onChange={setBudget} />
        </div>
        <ProviderDisclosure />
        {sources.data && !readyCount && <p className="text-sm text-contested">Chưa có nguồn nào sẵn sàng. Thêm nguồn ở tab “Nguồn”.</p>}
      </form>
      {ask.error && <ErrorBox error={ask.error} onRetry={() => ask.mutate()} />}
      {runId && <RunView key={runId} runId={runId} />}
    </div>
  )
}
