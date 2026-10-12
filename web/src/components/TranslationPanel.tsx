import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { AlertTriangle, Download, Languages } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { ApiError, api, type TranslationDetail } from '../api'
import { currentLanguage, t } from '../i18n'
import { formatMs } from '../lib/mediaUpload'
import { ErrorBox, Notice, ProgressBar, Spinner } from './ui'

/** Ngôn ngữ giao diện → ngôn ngữ đích mặc định của bản dịch. */
const UI_TO_TARGET: Record<string, string> = { zh: 'zh-Hans' }

const FLAG_LABEL: Record<string, () => string> = {
  numbers_changed: () => t('Số liệu khác bản gốc — kiểm tra lại'),
  empty: () => t('Không dịch được đoạn này'),
}

/**
 * Bản dịch máy của nguồn, theo từng đoạn lá, chỉ để đọc. Trích dẫn và kiểm chứng luôn dựa trên bản gốc;
 * đoạn có số liệu khác bản gốc được cảnh báo.
 */
export function TranslationPanel({
  sourceId,
  parseRevisionId,
  citedNodeId,
  onSeek,
}: {
  sourceId: string
  parseRevisionId: string
  citedNodeId?: string
  onSeek?: (ms: number) => void
}) {
  const queryClient = useQueryClient()
  const languages = useQuery({ queryKey: ['translation-languages'], queryFn: () => api.listTranslationLanguages(), staleTime: Infinity })
  const preferred = UI_TO_TARGET[currentLanguage()] ?? currentLanguage()
  const [target, setTarget] = useState<string | null>(null)
  const lang = target ?? (languages.data?.some((l) => l.code === preferred) && preferred !== 'vi' ? preferred : 'en')
  const [layout, setLayout] = useState<'side' | 'translation'>('side')

  const detail = useQuery({
    queryKey: ['translation', sourceId, parseRevisionId, lang],
    queryFn: () => api.getTranslation(sourceId, parseRevisionId, lang),
    retry: false,
    refetchInterval: (query) => (['queued', 'running'].includes(query.state.data?.status ?? '') ? 1500 : false),
  })
  const notStarted = detail.error instanceof ApiError && detail.error.status === 404
  const start = useMutation({
    mutationFn: () => api.requestTranslation(sourceId, lang),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['translation', sourceId, parseRevisionId, lang] }),
  })

  const data = detail.data
  const busy = data && ['queued', 'running'].includes(data.status)
  return (
    <div className="space-y-3">
      <div className="flex flex-wrap items-center gap-2">
        <Languages className="size-4 text-muted" aria-hidden />
        <label className="sr-only" htmlFor="translation-target">
          {t('Dịch sang')}
        </label>
        <select
          id="translation-target"
          className="input w-auto py-1"
          value={lang}
          onChange={(e) => setTarget(e.target.value)}
          disabled={!languages.data}
        >
          {(languages.data ?? [{ code: lang, name: lang }]).map((l) => (
            <option key={l.code} value={l.code}>
              {l.name}
            </option>
          ))}
        </select>
        {(notStarted || data?.status === 'failed') && (
          <button className="btn btn-primary py-1" onClick={() => start.mutate()} disabled={start.isPending}>
            {start.isPending && <Spinner />} {data?.status === 'failed' ? t('Dịch lại') : t('Dịch tài liệu')}
          </button>
        )}
        {data?.status === 'succeeded' && (
          <>
            <div className="ml-auto inline-flex rounded-md border border-line p-0.5" role="radiogroup" aria-label={t('Cách hiển thị')}>
              {(
                [
                  ['side', t('Song song')],
                  ['translation', t('Chỉ bản dịch')],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="radio"
                  aria-checked={layout === id}
                  onClick={() => setLayout(id)}
                  className={clsx('rounded px-2 py-0.5 text-xs', layout === id ? 'bg-accent text-accent-fg' : 'text-muted')}
                >
                  {label}
                </button>
              ))}
            </div>
            <button className="btn btn-ghost p-1.5" onClick={() => download(data)} title={t('Tải bản dịch (.txt)')} aria-label={t('Tải bản dịch (.txt)')}>
              <Download className="size-4" />
            </button>
          </>
        )}
      </div>

      {notStarted && !start.isPending && (
        <p className="text-sm text-muted">
          {t('Chưa có bản dịch sang ngôn ngữ này. Nội dung nguồn sẽ được gửi tới nhà cung cấp LLM để dịch.')}
        </p>
      )}
      {start.error && <ErrorBox error={start.error} />}
      {detail.error && !notStarted && <ErrorBox error={detail.error} onRetry={() => detail.refetch()} />}
      {data?.status === 'failed' && data.error && <ErrorBox error={new ApiError(500, data.error)} />}
      {busy && (
        <div className="space-y-1">
          <ProgressBar value={data.progress} label={t('Tiến độ dịch')} />
          <p className="text-xs text-muted">
            {t('Đang dịch {0}/{1} đoạn…', { 0: data.segments.length, 1: data.segment_count })}
          </p>
        </div>
      )}
      {data && data.segments.length > 0 && (
        <>
          <Notice tone={data.flagged_count ? 'warn' : 'info'}>
            {t('Bản dịch máy ({0}), chỉ để đọc. Trích dẫn và kiểm chứng luôn dựa trên bản gốc.', { 0: data.model ?? 'LLM' })}
            {data.flagged_count > 0 && ` ${t('{0} đoạn có số liệu khác bản gốc — đã đánh dấu.', { 0: data.flagged_count })}`}
          </Notice>
          <SegmentList detail={data} layout={layout} citedNodeId={citedNodeId} onSeek={onSeek} />
        </>
      )}
    </div>
  )
}

function SegmentList({
  detail,
  layout,
  citedNodeId,
  onSeek,
}: {
  detail: TranslationDetail
  layout: 'side' | 'translation'
  citedNodeId?: string
  onSeek?: (ms: number) => void
}) {
  const cited = useRef<HTMLLIElement>(null)
  useEffect(() => {
    cited.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [citedNodeId, detail.segments.length])
  return (
    <ol className="space-y-2">
      {detail.segments.map((s) => {
        const isCited = s.node_id === citedNodeId
        return (
          <li
            key={s.node_id}
            ref={isCited ? cited : undefined}
            className={clsx('rounded-md border p-3 text-sm', isCited ? 'border-highlight bg-highlight/40' : 'border-line', s.flags.length && 'border-contested/60')}
          >
            {(s.start_ms !== null || s.page_start !== null) && (
              <div className="mb-1 text-xs text-muted">
                {s.start_ms !== null && onSeek ? (
                  <button type="button" className="font-mono text-accent hover:underline" onClick={() => onSeek(s.start_ms!)}>
                    {formatMs(s.start_ms)}
                  </button>
                ) : s.page_start !== null ? (
                  t('Trang {0}', { 0: s.page_start })
                ) : null}
              </div>
            )}
            <div className={clsx('grid gap-3', layout === 'side' && 'md:grid-cols-2')}>
              {layout === 'side' && <p className="whitespace-pre-wrap text-muted" lang="und">{s.original}</p>}
              <p className="whitespace-pre-wrap" lang={detail.target_lang}>
                {s.text || <span className="italic text-muted">{s.original}</span>}
              </p>
            </div>
            {s.flags.map((flag) => (
              <p key={flag} className="mt-1 inline-flex items-center gap-1 text-xs text-contested">
                <AlertTriangle className="size-3.5" aria-hidden /> {FLAG_LABEL[flag]?.() ?? flag}
              </p>
            ))}
          </li>
        )
      })}
    </ol>
  )
}

function download(detail: TranslationDetail) {
  const body = detail.segments.map((s) => s.text || s.original).join('\n\n')
  const url = URL.createObjectURL(new Blob([body], { type: 'text/plain;charset=utf-8' }))
  const a = document.createElement('a')
  a.href = url
  a.download = `translation-${detail.target_lang}.txt`
  a.click()
  URL.revokeObjectURL(url)
}
