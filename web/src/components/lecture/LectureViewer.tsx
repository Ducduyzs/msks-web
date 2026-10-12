import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { ChevronLeft, ChevronRight, ExternalLink, Pause, Pencil, Play, X } from 'lucide-react'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  ApiError,
  api,
  type EvidenceRef,
  type FrameRegion,
  type MediaAssetRef,
  type Source,
  type Timeline,
  type TranscriptSegment,
} from '../../api'
import { EVIDENCE_ROLE, LANGUAGE, MODALITY, SOURCE_KIND, SOURCE_STATUS_HINT, SUPPORT_BASIS, qualityFlag } from '../../lib/labels'
import { formatMs } from '../../lib/mediaUpload'
import { safeExternalUrl } from '../../lib/security'
import { SourceStatusBadge } from '../badges'
import { TranslationPanel } from '../TranslationPanel'
import { ErrorBox, Loading, Notice, Pill, Spinner } from '../ui'
import { t, msg, translated } from '../../i18n'

export interface LectureTarget {
  sourceId: string
  alias: string
  parseRevisionId: string
  evidence?: EvidenceRef
}

/**
 * Trình xem bài giảng (LECTURE_ARCHITECTURE.md mục 9): ảnh bảng/slide + âm thanh theo đoạn + bản chép chạy theo thời gian.
 * Mặc định backend không giữ video gốc — chỉ âm thanh FLAC và khung hình làm bằng chứng — nên trình xem dựng lại từ đó.
 */
export function LectureDrawer({
  target,
  source,
  onClose,
  onOpenRevision,
}: {
  target: LectureTarget
  source?: Source
  onClose: () => void
  onOpenRevision: (parseRevisionId: string) => void
}) {
  const evidence = target.evidence
  const deleted = !!evidence?.source_deleted || !!source?.deleted_at
  const timeline = useQuery({
    queryKey: ['timeline', target.sourceId, target.parseRevisionId],
    queryFn: () => api.getTimeline(target.sourceId, target.parseRevisionId),
    enabled: !deleted,
    retry: (count, error) => !(error instanceof ApiError && [404, 410].includes(error.status)) && count < 1,
  })
  const closeRef = useRef<HTMLButtonElement>(null)
  const goneByServer = timeline.error instanceof ApiError && timeline.error.status === 410
  const latest = source?.revision?.parse_revision_id
  const outdated = !!latest && latest !== target.parseRevisionId

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

  const startMs = evidence?.locator?.start_ms ?? 0
  return (
    <div className="fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label={t('Bài giảng {0}', { 0: target.alias })}>
      <button className="absolute inset-0 bg-black/30" aria-label={t('Đóng trình xem')} tabIndex={-1} onClick={onClose} />
      <aside className="relative flex h-full w-full max-w-5xl flex-col border-l border-line bg-surface shadow-xl">
        <header className="flex items-start gap-3 border-b border-line p-4">
          <div className="min-w-0 flex-1">
            <div className="mb-1 flex flex-wrap items-center gap-2">
              <Pill className="bg-accent-soft text-accent">{target.alias}</Pill>
              {source && <SourceStatusBadge status={source.status} />}
              {source && (
                <span className="text-xs text-muted">
                  {SOURCE_KIND[source.kind]}
                  {source.language && ` · ${LANGUAGE[source.language]}`}
                  {source.duration_ms ? ` · ${formatMs(source.duration_ms)}` : ''}
                </span>
              )}
            </div>
            <h2 className="font-semibold leading-snug">{source?.title ?? t('Bài giảng {0}', { 0: target.alias })}</h2>
            <p className="mt-1 font-mono text-xs text-muted">{t('revision')} {target.parseRevisionId}</p>
            {safeExternalUrl(source?.url) && !deleted && (
              <a
                href={youtubeAt(safeExternalUrl(source?.url)!, startMs)}
                target="_blank"
                rel="noreferrer noopener"
                className="mt-1 inline-flex items-center gap-1 text-sm text-accent hover:underline"
              >
                {t('Mở bản gốc')}{startMs ? t(' tại {0}', { 0: formatMs(startMs) }) : ''} <ExternalLink className="size-3.5" />
              </a>
            )}
          </div>
          <button ref={closeRef} className="btn btn-ghost p-1.5" onClick={onClose} aria-label={t('Đóng')}>
            <X className="size-5" />
          </button>
        </header>

        {evidence && !deleted && <EvidenceBanner evidence={evidence} />}

        <div className="flex-1 overflow-y-auto p-4">
          {deleted || goneByServer ? (
            <Notice tone="warn" title={t('Nguồn đã bị xóa')}>
              {t('Tham chiếu vẫn được giữ trong lịch sử, nhưng âm thanh, khung hình và bản chép đã bị xóa và không mở lại được.')}
            </Notice>
          ) : (
            <div className="space-y-4">
              {outdated && (
                <Notice title={t('Đang xem phiên bản cũ')}>
                  {evidence ? t('Lượt hỏi này dùng bản chép tại thời điểm chạy; bản chép đã được sửa sau đó.') : t('Bản chép đã có phiên bản mới hơn.')}{' '}
                  <button className="font-medium text-accent underline" onClick={() => onOpenRevision(latest!)}>
                    {t('Mở phiên bản mới nhất')}
                  </button>
                </Notice>
              )}
              {source && SOURCE_STATUS_HINT[source.status] && <Notice tone={source.status === 'review_required' ? 'warn' : 'info'}>{SOURCE_STATUS_HINT[source.status]}</Notice>}
              {timeline.isPending && <Loading />}
              {timeline.error && <ErrorBox error={timeline.error} onRetry={() => timeline.refetch()} />}
              {timeline.data && (
                <LectureBody
                  key={timeline.data.parse_revision_id}
                  timeline={timeline.data}
                  sourceId={target.sourceId}
                  startMs={startMs}
                  cited={new Set(evidence?.locator?.items.map((i) => i.id) ?? [])}
                  citedNodeId={evidence?.node_id}
                  editable={!outdated}
                />
              )}
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}

function youtubeAt(url: string, ms: number) {
  if (!ms || !/youtube\.com|youtu\.be/.test(url)) return url
  return `${url}${url.includes('?') ? '&' : '?'}t=${Math.floor(ms / 1000)}s`
}

function EvidenceBanner({ evidence }: { evidence: EvidenceRef }) {
  const locator = evidence.locator
  const flags = [...new Set(locator?.items.flatMap((i) => i.flags) ?? [])]
  const basis = evidence.transcription_state === 'reviewed' ? SUPPORT_BASIS.reviewed_transcript : evidence.transcription_state === 'automatic' ? SUPPORT_BASIS.automatic_transcript : null
  return (
    <div className="space-y-1.5 border-b border-line bg-surface-2 px-4 py-3 text-sm">
      <p>
        <span className="font-medium">{EVIDENCE_ROLE[evidence.role]}</span>
        <span className="text-muted">
          {evidence.modality && ` · ${MODALITY[evidence.modality]}`}
          {locator?.start_ms !== undefined && ` · ${formatMs(locator.start_ms)}–${formatMs(locator.end_ms)}`}
        </span>
        {basis && (
          <Pill className="ml-2 bg-contested-soft text-contested" title={basis.hint}>
            {basis.label}
          </Pill>
        )}
      </p>
      <blockquote className="rounded-md border-l-4 border-highlight bg-surface px-3 py-2">{evidence.evidence_snapshot}</blockquote>
      {flags.length > 0 && <p className="text-xs text-contested">{t('Cảnh báo trích xuất:')} {flags.map(qualityFlag).join(', ')}</p>}
      {locator && !locator.items.length && <p className="text-xs text-muted">{t('Không ánh xạ được về đoạn/vùng cụ thể; hiển thị đoạn trích đã kiểm chứng.')}</p>}
    </div>
  )
}

// ----------------------------------------------------------------- phát lại

/** Đồng hồ chung: âm thanh (nếu có) điều khiển thời gian; không có thì kéo thanh thời gian/bấm vào câu. */
function usePlayback(sourceId: string, audio: MediaAssetRef[], startMs: number) {
  const [currentMs, setCurrentMs] = useState(startMs)
  const [playing, setPlaying] = useState(false)
  const element = useRef<HTMLAudioElement>(null)
  const pendingSeek = useRef<number | null>(null)
  const chunk = audio.find((a) => (a.meta.start_ms ?? 0) <= currentMs && currentMs < (a.meta.end_ms ?? Infinity)) ?? audio[audio.length - 1]
  const queryClient = useQueryClient()
  const url = useQuery({
    queryKey: ['media-url', sourceId, chunk?.id],
    queryFn: () => api.getMediaUrl(sourceId, chunk!.id),
    enabled: !!chunk,
    staleTime: 240_000, // URL hết hạn sau 300 s
  })

  const seek = useCallback(
    (ms: number) => {
      setCurrentMs(ms)
      const audioEl = element.current
      if (!audioEl || !chunk) return
      const start = chunk.meta.start_ms ?? 0
      const end = chunk.meta.end_ms ?? Infinity
      if (ms >= start && ms < end) audioEl.currentTime = (ms - start) / 1000
      else pendingSeek.current = ms // đoạn âm thanh khác: áp dụng khi nạp xong
    },
    [chunk],
  )

  const onLoaded = () => {
    const audioEl = element.current
    if (!audioEl || !chunk) return
    const target = pendingSeek.current ?? currentMs
    pendingSeek.current = null
    audioEl.currentTime = Math.max(0, (target - (chunk.meta.start_ms ?? 0)) / 1000)
    if (playing) void audioEl.play().catch(() => setPlaying(false))
  }

  const toggle = () => {
    const audioEl = element.current
    if (!audioEl) return
    if (audioEl.paused) void audioEl.play().then(() => setPlaying(true)).catch(() => setPlaying(false))
    else {
      audioEl.pause()
      setPlaying(false)
    }
  }

  const audioProps = chunk
    ? {
        ref: element,
        src: url.data?.url,
        preload: 'metadata' as const,
        onLoadedMetadata: onLoaded,
        onTimeUpdate: () => {
          const audioEl = element.current
          if (audioEl && !audioEl.paused) setCurrentMs((chunk.meta.start_ms ?? 0) + audioEl.currentTime * 1000)
        },
        onEnded: () => {
          const next = audio.find((a) => (a.meta.start_ms ?? 0) >= (chunk.meta.end_ms ?? Infinity))
          if (next) setCurrentMs(next.meta.start_ms ?? 0)
          else setPlaying(false)
        },
        // URL ký đã hết hạn → xin URL mới.
        onError: () => queryClient.invalidateQueries({ queryKey: ['media-url', sourceId, chunk.id] }),
      }
    : null
  return { currentMs, seek, playing, toggle, audioProps, hasAudio: !!chunk, audioError: url.error }
}

function LectureBody({
  timeline,
  sourceId,
  startMs,
  cited,
  editable,
  citedNodeId,
}: {
  timeline: Timeline
  sourceId: string
  startMs: number
  citedNodeId?: string
  cited: Set<string>
  editable: boolean
}) {
  const audio = useMemo(() => timeline.assets.filter((a) => a.kind === 'audio').sort((a, b) => (a.meta.start_ms ?? 0) - (b.meta.start_ms ?? 0)), [timeline])
  const frames = useMemo(() => timeline.assets.filter((a) => a.kind === 'frame').sort((a, b) => (a.meta.timestamp_ms ?? 0) - (b.meta.timestamp_ms ?? 0)), [timeline])
  const playback = usePlayback(sourceId, audio, startMs)
  const duration = timeline.duration_ms ?? Math.max(0, ...timeline.segments.map((s) => s.end_ms))
  const [editing, setEditing] = useState(false)
  const [panel, setPanel] = useState<'transcript' | 'translation'>('transcript')
  const [edits, setEdits] = useState<{ segments: Map<string, string>; regions: Map<string, string> }>({ segments: new Map(), regions: new Map() })
  const queryClient = useQueryClient()
  const save = useMutation({
    mutationFn: () =>
      api.createTranscriptRevision(sourceId, {
        base_revision_id: timeline.parse_revision_id,
        segments: [...edits.segments].map(([id, text]) => ({ id, text })),
        regions: [...edits.regions].map(([id, text]) => ({ id, text })),
      }),
    onSuccess: () => {
      setEditing(false)
      setEdits({ segments: new Map(), regions: new Map() })
      queryClient.invalidateQueries({ queryKey: ['sources'] })
    },
  })
  const editCount = edits.segments.size + edits.regions.size
  const setEdit = (kind: 'segments' | 'regions', id: string, text: string, original: string) =>
    setEdits((previous) => {
      const next = new Map(previous[kind])
      if (text === original) next.delete(id)
      else next.set(id, text)
      return { ...previous, [kind]: next }
    })

  return (
    <div className="space-y-4">
      <CoverageLine timeline={timeline} />
      <div className="grid gap-4 lg:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        <div className="space-y-3">
          <BoardPanel
            sourceId={sourceId}
            frames={frames}
            regions={timeline.regions}
            currentMs={playback.currentMs}
            cited={cited}
            onSeek={playback.seek}
            editing={editing}
            edits={edits.regions}
            onEdit={(id, text, original) => setEdit('regions', id, text, original)}
          />
          <div className="card flex flex-wrap items-center gap-3 p-3">
            {playback.audioProps && <audio {...playback.audioProps} />}
            <button
              type="button"
              className="btn btn-primary p-2"
              onClick={playback.toggle}
              disabled={!playback.hasAudio}
              aria-label={playback.playing ? t('Tạm dừng') : t('Phát')}
            >
              {playback.playing ? <Pause className="size-4" /> : <Play className="size-4" />}
            </button>
            <span className="font-mono text-xs tabular-nums text-muted">
              {formatMs(playback.currentMs)} / {formatMs(duration)}
            </span>
            <input
              type="range"
              min={0}
              max={Math.max(1, duration)}
              step={500}
              value={Math.min(playback.currentMs, duration)}
              onChange={(e) => playback.seek(Number(e.target.value))}
              className="min-w-40 flex-1 accent-[var(--accent)]"
              aria-label={t('Vị trí trong bài giảng')}
            />
            {!playback.hasAudio && (
              <p className="w-full text-xs text-muted">
                {t('Không có âm thanh để phát (')}{timeline.capabilities?.original_playback_available === 'external' ? t('nguồn chỉ có phụ đề') : t('chưa có đoạn âm thanh')}{t('). Kéo thanh thời gian hoặc bấm vào câu để xem bảng tại thời điểm đó.')}
              </p>
            )}
            {playback.audioError !== null && playback.audioError !== undefined && <ErrorBox error={playback.audioError} />}
          </div>
        </div>
        <div className="space-y-2">
          <div className="flex flex-wrap items-center gap-2">
            <div className="inline-flex rounded-md border border-line p-0.5" role="tablist">
              {(
                [
                  ['transcript', t('Bản chép lời')],
                  ['translation', t('Bản dịch')],
                ] as const
              ).map(([id, label]) => (
                <button
                  key={id}
                  type="button"
                  role="tab"
                  aria-selected={panel === id}
                  onClick={() => (setPanel(id), setEditing(false))}
                  className={clsx('rounded px-3 py-1 text-sm font-medium', panel === id ? 'bg-accent text-accent-fg' : 'text-muted')}
                >
                  {label}
                </button>
              ))}
            </div>
            {panel === 'transcript' && <span className="text-xs text-muted">{timeline.segments.length} {t('đoạn')}</span>}
            {editable && panel === 'transcript' &&
              (editing ? (
                <div className="ml-auto flex gap-1">
                  <button type="button" className="btn btn-ghost" onClick={() => (setEditing(false), setEdits({ segments: new Map(), regions: new Map() }))}>
                    {t('Bỏ')}
                  </button>
                  <button type="button" className="btn btn-primary" disabled={!editCount || save.isPending} onClick={() => save.mutate()}>
                    {save.isPending && <Spinner />} {t('Lưu')} {editCount ? t('{0} chỗ sửa', { 0: editCount }) : ''}
                  </button>
                </div>
              ) : (
                <button type="button" className="btn btn-outline ml-auto" onClick={() => setEditing(true)}>
                  <Pencil className="size-3.5" /> {t('Sửa bản chép')}
                </button>
              ))}
          </div>
          {editing && (
            <p className="text-xs text-muted">
              {t('Lưu tạo phiên bản bản chép mới và lập chỉ mục lại; các lượt hỏi cũ vẫn trỏ về phiên bản cũ. Đoạn đã sửa được tính là đã đối chiếu.')}
            </p>
          )}
          {save.error && <ErrorBox error={save.error} />}
          {save.isSuccess && (
            <Notice title={t('Đang lập chỉ mục lại')}>{t('Phiên bản mới sẽ thay thế khi xử lý xong; đóng và mở lại nguồn để xem.')}</Notice>
          )}
          {panel === 'translation' && (
            <TranslationPanel sourceId={sourceId} parseRevisionId={timeline.parse_revision_id} citedNodeId={citedNodeId} onSeek={playback.seek} />
          )}
          {panel === 'transcript' && <TranscriptList
            segments={timeline.segments}
            currentMs={playback.currentMs}
            cited={cited}
            onSeek={playback.seek}
            editing={editing}
            edits={edits.segments}
            onEdit={(id, text, original) => setEdit('segments', id, text, original)}
          />}
        </div>
      </div>
    </div>
  )
}

function CoverageLine({ timeline }: { timeline: Timeline }) {
  const c = timeline.coverage
  if (!c) return null
  const parts = [
    c.captions_used ? t('Lời: phụ đề người dùng cung cấp') : c.audio_read ? t('Lời: chép tự động{0}', { 0: timeline.versions.asr?.model ? ` (${timeline.versions.asr.model.split('/').pop()})` : '' }) : t('Không có lời'),
    c.speech_ratio !== null && c.speech_ratio !== undefined ? t('phủ {0}% thời lượng', { 0: Math.round(c.speech_ratio * 100) }) : null,
    c.segments_flagged ? t('{0}/{1} đoạn bị cảnh báo', { 0: c.segments_flagged, 1: c.segments }) : null,
    c.visual_read ? t('{0} khung hình, {1} vùng chữ', { 0: c.frames_selected ?? 0, 1: c.board_regions ?? 0 }) : t('Không có hình'),
    c.visual_incomplete_from_ms ? t('chưa đọc hình từ {0}', { 0: formatMs(c.visual_incomplete_from_ms) }) : null,
  ].filter(Boolean)
  return <p className="text-xs text-muted">{parts.join(' · ')}</p>
}

function BoardPanel({
  sourceId,
  frames,
  regions,
  currentMs,
  cited,
  onSeek,
  editing,
  edits,
  onEdit,
}: {
  sourceId: string
  frames: MediaAssetRef[]
  regions: FrameRegion[]
  currentMs: number
  cited: Set<string>
  onSeek: (ms: number) => void
  editing: boolean
  edits: Map<string, string>
  onEdit: (id: string, text: string, original: string) => void
}) {
  // Khung đang hiển thị: khung cuối cùng xuất hiện trước thời điểm hiện tại.
  const index = Math.max(0, frames.findLastIndex((f) => (f.meta.timestamp_ms ?? 0) <= currentMs))
  const frame = frames[index]
  const url = useQuery({
    queryKey: ['media-url', sourceId, frame?.id],
    queryFn: () => api.getMediaUrl(sourceId, frame!.id),
    enabled: !!frame,
    staleTime: 240_000,
  })
  if (!frame) {
    return <div className="card flex aspect-video items-center justify-center p-6 text-sm text-muted">{t('Không có hình bảng/slide cho nguồn này.')}</div>
  }
  const width = frame.meta.width ?? 1280
  const height = frame.meta.height ?? 720
  const onFrame = regions.filter((r) => r.frame_asset_id === frame.id)
  return (
    <div className="space-y-2">
      <div className="relative overflow-hidden rounded-lg border border-line bg-surface-2">
        {url.data ? (
          <img src={url.data.url} alt={t('Khung hình lúc {0}', { 0: formatMs(frame.meta.timestamp_ms) })} className="block aspect-video w-full object-contain" />
        ) : (
          <div className="flex aspect-video items-center justify-center">{url.error ? <ErrorBox error={url.error} /> : <Spinner />}</div>
        )}
        <svg viewBox={`0 0 ${width} ${height}`} className="pointer-events-none absolute inset-0 size-full" preserveAspectRatio="xMidYMid meet" aria-hidden>
          {onFrame.map((r) => (
            <rect
              key={r.id}
              x={r.bbox[0]}
              y={r.bbox[1]}
              width={r.bbox[2] - r.bbox[0]}
              height={r.bbox[3] - r.bbox[1]}
              rx={6}
              className={cited.has(r.id) ? 'fill-[var(--highlight)] stroke-[var(--contested)]' : 'fill-transparent stroke-[var(--accent)]'}
              fillOpacity={cited.has(r.id) ? 0.35 : 0}
              strokeOpacity={cited.has(r.id) ? 1 : r.included ? 0.35 : 0.15}
              strokeWidth={cited.has(r.id) ? 4 : 2}
              strokeDasharray={r.included ? undefined : '8 6'}
            />
          ))}
        </svg>
      </div>
      <div className="flex items-center gap-2 text-xs text-muted">
        <button type="button" className="btn btn-ghost p-1" disabled={index === 0} onClick={() => onSeek(frames[index - 1].meta.timestamp_ms ?? 0)} aria-label={t('Khung trước')}>
          <ChevronLeft className="size-4" />
        </button>
        <span>
          {t('Khung')} {index + 1}/{frames.length} {t('· từ')} {formatMs(frame.meta.timestamp_ms)}
        </span>
        <button
          type="button"
          className="btn btn-ghost p-1"
          disabled={index >= frames.length - 1}
          onClick={() => onSeek(frames[index + 1].meta.timestamp_ms ?? 0)}
          aria-label={t('Khung sau')}
        >
          <ChevronRight className="size-4" />
        </button>
      </div>
      {onFrame.length > 0 && (
        <ul className="space-y-1 text-sm" aria-label={t('Chữ đọc được trên khung hình')}>
          {onFrame.map((r) => (
            <li key={r.id} className={clsx('rounded px-2 py-1', cited.has(r.id) ? 'bg-highlight' : 'bg-surface-2', !r.included && 'opacity-60')}>
              {editing ? (
                <input
                  className="input py-1 text-sm"
                  value={edits.get(r.id) ?? r.text}
                  onChange={(e) => onEdit(r.id, e.target.value, r.text)}
                  aria-label={t('Sửa chữ trên bảng: {0}', { 0: r.text })}
                />
              ) : (
                <span>{edits.get(r.id) ?? r.text}</span>
              )}
              <span className="ml-2 text-xs text-muted">
                {r.review_state === 'reviewed' ? t('đã sửa') : `OCR${r.confidence !== null ? ` ${Math.round(r.confidence * 100)}%` : ''}`}
                {!r.included && t(' · độ tin thấp, không dùng làm bằng chứng')}
                {r.flags.length > 0 && ` · ${r.flags.map(qualityFlag).join(', ')}`}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

const ORIGIN: Record<string, string> = translated({ asr: msg('chép tự động'), human_caption: msg('phụ đề'), manual_edit: msg('đã sửa') })

function TranscriptList({
  segments,
  currentMs,
  cited,
  onSeek,
  editing,
  edits,
  onEdit,
}: {
  segments: TranscriptSegment[]
  currentMs: number
  cited: Set<string>
  onSeek: (ms: number) => void
  editing: boolean
  edits: Map<string, string>
  onEdit: (id: string, text: string, original: string) => void
}) {
  const active = segments.find((s) => s.start_ms <= currentMs && currentMs < s.end_ms)?.id
  const activeRef = useRef<HTMLLIElement>(null)
  useEffect(() => {
    if (!editing) activeRef.current?.scrollIntoView({ block: 'nearest', behavior: 'smooth' })
  }, [active, editing])
  if (!segments.length) return <p className="text-sm text-muted">{t('Không có lời nào được chép.')}</p>
  return (
    <ol className="max-h-[60vh] space-y-1 overflow-y-auto pr-1">
      {segments.map((s) => (
        <li
          key={s.id}
          ref={s.id === active ? activeRef : undefined}
          className={clsx(
            'rounded-md px-2 py-1.5 text-sm',
            cited.has(s.id) ? 'bg-highlight' : s.id === active ? 'bg-accent-soft' : 'hover:bg-surface-2',
          )}
          aria-current={s.id === active ? 'true' : undefined}
        >
          <div className="flex items-baseline gap-2">
            <button type="button" className="shrink-0 font-mono text-xs text-accent hover:underline" onClick={() => onSeek(s.start_ms)}>
              {formatMs(s.start_ms)}
            </button>
            {editing ? (
              <textarea
                className="input min-h-14 flex-1 py-1 text-sm"
                value={edits.get(s.id) ?? s.text}
                onChange={(e) => onEdit(s.id, e.target.value, s.text)}
                aria-label={t('Sửa đoạn lúc {0}', { 0: formatMs(s.start_ms) })}
              />
            ) : (
              <button type="button" className="min-w-0 flex-1 text-left leading-relaxed" onClick={() => onSeek(s.start_ms)}>
                {edits.get(s.id) ?? s.text}
              </button>
            )}
          </div>
          {(s.flags.length > 0 || s.origin !== 'asr') && (
            <div className="mt-1 flex flex-wrap gap-1 pl-12">
              {s.origin !== 'asr' && <Pill className="bg-surface-2 text-muted">{ORIGIN[s.origin] ?? s.origin}</Pill>}
              {s.flags.map((f) => (
                <Pill key={f} className="bg-contested-soft text-contested">
                  {qualityFlag(f)}
                </Pill>
              ))}
            </div>
          )}
        </li>
      ))}
    </ol>
  )
}
