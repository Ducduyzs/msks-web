import { useQuery } from '@tanstack/react-query'
import clsx from 'clsx'
import { ExternalLink, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState, type ReactNode, type RefObject } from 'react'
import { ApiError, api, type EvidenceRef, type Source } from '../api'
import { CodePointIndex, segment } from '../lib/codepoints'
import { EVIDENCE_ROLE, SOURCE_KIND, SOURCE_TYPE, isLecture, pageLabel } from '../lib/labels'
import { safeExternalUrl } from '../lib/security'
import { LectureDrawer } from './lecture/LectureViewer'
import { TranslationPanel } from './TranslationPanel'
import { ErrorBox, Loading, Notice, Pill } from './ui'
import { ViewerContext } from './viewerContext'
import { t } from '../i18n'

interface Target {
  sourceId: string
  alias: string
  parseRevisionId: string
  evidence?: EvidenceRef
}

/** Trình xem nguồn dạng ngăn kéo bên phải, dùng chung cho cả workspace. Bài giảng mở trình xem riêng theo thời gian. */
export function ViewerProvider({ sources, children }: { sources: Source[]; children: ReactNode }) {
  const [target, setTarget] = useState<(Target & { lecture: boolean }) | null>(null)
  const openSource = useCallback((source: Source) => {
    if (source.revision) {
      setTarget({ sourceId: source.id, alias: source.alias, parseRevisionId: source.revision.parse_revision_id, lecture: isLecture(source.kind) })
    }
  }, [])
  const openEvidence = useCallback(
    (evidence: EvidenceRef) =>
      setTarget({
        sourceId: evidence.source_id,
        alias: evidence.source_alias,
        parseRevisionId: evidence.parse_revision_id,
        evidence,
        lecture: !!evidence.modality && evidence.modality !== 'text',
      }),
    [],
  )
  const value = useMemo(() => ({ openSource, openEvidence }), [openSource, openEvidence])
  const source = sources.find((s) => s.id === target?.sourceId)
  const close = useCallback(() => setTarget(null), [])

  return (
    <ViewerContext.Provider value={value}>
      {children}
      {target &&
        (target.lecture ? (
          <LectureDrawer
            target={target}
            source={source}
            onClose={close}
            onOpenRevision={(parseRevisionId) => setTarget({ ...target, parseRevisionId, evidence: undefined })}
          />
        ) : (
          <SourceDrawer target={target} source={source} onClose={close} />
        ))}
    </ViewerContext.Provider>
  )
}

function SourceDrawer({ target, source, onClose }: { target: Target; source?: Source; onClose: () => void }) {
  const evidence = target.evidence
  const deleted = !!evidence?.source_deleted || !!source?.deleted_at
  const [view, setView] = useState<'original' | 'translation'>('original')
  const content = useQuery({
    queryKey: ['source-content', target.sourceId, target.parseRevisionId],
    queryFn: () => api.getSourceContent(target.sourceId, target.parseRevisionId),
    enabled: !deleted,
    retry: (count, error) => !(error instanceof ApiError && [404, 410].includes(error.status)) && count < 1,
  })
  const markRef = useRef<HTMLElement>(null)
  const closeRef = useRef<HTMLButtonElement>(null)
  const index = useMemo(() => (content.data ? new CodePointIndex(content.data.text) : null), [content.data])
  const exact = !!evidence && evidence.position === 'exact'
  const goneByServer = content.error instanceof ApiError && content.error.status === 410

  useEffect(() => {
    markRef.current?.scrollIntoView({ block: 'center', behavior: 'smooth' })
  }, [index, evidence])

  // Bàn phím: Esc để đóng, focus vào nút đóng khi mở, trả focus khi đóng.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    closeRef.current?.focus()
    const onKey = (event: KeyboardEvent) => event.key === 'Escape' && onClose()
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('keydown', onKey)
      previous?.focus?.()
    }
  }, [onClose])

  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label={t('Nguồn {0}', { 0: target.alias })}>
      <button className="absolute inset-0 bg-black/30" aria-label={t('Đóng trình xem')} tabIndex={-1} onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-xl flex-col border-l border-line bg-surface shadow-xl">
        <header className="flex items-start gap-3 border-b border-line p-4">
          <div className="min-w-0 flex-1">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Pill className="bg-accent-soft text-accent">{target.alias}</Pill>
              {source && (
                <span className="text-xs text-muted">
                  {SOURCE_KIND[source.kind]} · {SOURCE_TYPE[source.source_type]}
                </span>
              )}
            </div>
            <h2 className="font-semibold leading-snug">{source?.title ?? t('Nguồn {0}', { 0: target.alias })}</h2>
            {source?.authors.length ? <p className="mt-0.5 truncate text-sm text-muted">{source.authors.join(', ')}</p> : null}
            <p className="mt-1 font-mono text-xs text-muted">{t('revision')} {target.parseRevisionId}</p>
            {safeExternalUrl(source?.url) && !deleted && (
              <a href={safeExternalUrl(source?.url)!} target="_blank" rel="noreferrer noopener" className="mt-1 inline-flex items-center gap-1 text-sm text-accent hover:underline">
                {t('Mở bản gốc')} <ExternalLink className="size-3.5" />
              </a>
            )}
          </div>
          <button ref={closeRef} className="btn btn-ghost p-1.5" onClick={onClose} aria-label={t('Đóng')}>
            <X className="size-5" />
          </button>
        </header>

        {evidence && !deleted && (
          <div className="border-b border-line bg-surface-2 px-4 py-3 text-sm">
            <span className="font-medium">{EVIDENCE_ROLE[evidence.role]}</span>
            <span className="text-muted">
              {pageLabel(evidence.page_start, evidence.page_end) && ` · ${pageLabel(evidence.page_start, evidence.page_end)}`}
              {t(' · ký tự {0}–{1}', { 0: evidence.quote_start, 1: evidence.quote_end })}
            </span>
            <p className="mt-1 text-xs text-muted">
              <span className="mr-1 inline-block size-2.5 rounded-sm bg-highlight align-middle" /> {t('đoạn đã kiểm chứng')}
              <span className="ml-3 mr-1 inline-block size-2.5 rounded-sm bg-accent-soft align-middle" /> {t('phần còn lại của đoạn lá')}
            </p>
          </div>
        )}

        <div className="flex-1 overflow-y-auto p-4">
          {deleted || goneByServer ? (
            <Notice tone="warn" title={t('Nguồn đã bị xóa')}>
              {t('Tham chiếu vẫn được giữ trong lịch sử, nhưng nội dung đã bị ẩn và không được mở lại qua trình xem, kiểm toán hay xuất.')}
            </Notice>
          ) : view === 'translation' ? (
            <>
              <ViewTabs view={view} onChange={setView} />
              <TranslationPanel sourceId={target.sourceId} parseRevisionId={target.parseRevisionId} citedNodeId={evidence?.node_id} />
            </>
          ) : (
            <>
              <ViewTabs view={view} onChange={setView} />
              {evidence && !exact && (
                <div className="mb-4 space-y-2">
                  <Notice tone="warn" title={t('Không ánh xạ được vị trí chính xác')}>
                    {t('Hiển thị đoạn trích đã kiểm chứng')}
                    {pageLabel(evidence.page_start, evidence.page_end) && t(' và trang gần nhất ({0})', { 0: pageLabel(evidence.page_start, evidence.page_end) })}{t('; không tô sáng vị trí đoán.')}
                  </Notice>
                  <blockquote className="rounded-md border-l-4 border-highlight bg-surface-2 p-3 text-[15px] leading-7">
                    {evidence.evidence_snapshot}
                  </blockquote>
                </div>
              )}
              {content.isPending && <Loading />}
              {content.error && <ErrorBox error={content.error} onRetry={() => content.refetch()} />}
              {content.data && index && (
                <CanonicalText
                  index={index}
                  pages={content.data.pages}
                  leaf={exact ? [evidence.char_start, evidence.char_end] : undefined}
                  quote={exact ? [evidence.quote_start, evidence.quote_end] : undefined}
                  contradicting={evidence?.role === 'contradicting'}
                  markRef={markRef}
                />
              )}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

function ViewTabs({ view, onChange }: { view: 'original' | 'translation'; onChange: (view: 'original' | 'translation') => void }) {
  return (
    <div className="mb-4 inline-flex rounded-md border border-line p-0.5" role="tablist">
      {(
        [
          ['original', t('Bản gốc')],
          ['translation', t('Bản dịch')],
        ] as const
      ).map(([id, label]) => (
        <button
          key={id}
          type="button"
          role="tab"
          aria-selected={view === id}
          onClick={() => onChange(id)}
          className={clsx('rounded px-3 py-1 text-sm', view === id ? 'bg-accent text-accent-fg' : 'text-muted')}
        >
          {label}
        </button>
      ))}
    </div>
  )
}

type Range = [number, number]

function CanonicalText({
  index,
  pages,
  leaf,
  quote,
  contradicting,
  markRef,
}: {
  index: CodePointIndex
  pages: { page: number; char_start: number; char_end: number }[] | null
  leaf?: Range
  quote?: Range
  contradicting: boolean
  markRef: RefObject<HTMLElement | null>
}) {
  const blocks = pages ?? [{ page: 0, char_start: 0, char_end: index.length }]
  // Ref cuộn tới gắn vào đoạn quote đầu tiên, luôn bắt đầu tại quote[0].
  const markStart = quote?.[0] ?? -1
  return (
    <>
      {blocks.map((block) => {
        const segments = segment(block.char_start, block.char_end, leaf, quote)
        return (
          <section key={block.page} className="mb-6">
            {pages && <p className="label mb-2">{t('Trang')} {block.page}</p>}
            <div className="whitespace-pre-wrap text-[15px] leading-7">
              {segments.map(([start, end, kind]) => {
                const text = index.slice(start, end)
                if (kind === 'plain') return <span key={start}>{text}</span>
                const first = kind === 'quote' && start === markStart
                return (
                  <mark
                    key={start}
                    ref={first ? markRef : undefined}
                    className={clsx(
                      'text-fg',
                      kind === 'leaf' && 'bg-accent-soft',
                      kind === 'quote' && (contradicting ? 'rounded-sm bg-contested-soft ring-1 ring-contested' : 'rounded-sm bg-highlight'),
                    )}
                  >
                    {text}
                  </mark>
                )
              })}
            </div>
          </section>
        )
      })}
    </>
  )
}
