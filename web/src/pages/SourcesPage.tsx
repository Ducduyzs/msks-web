import { useMutation, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { Eye, FileUp, Link2, Trash2, Video } from 'lucide-react'
import { useRef, useState, type DragEvent, type FormEvent } from 'react'
import { useParams } from 'react-router-dom'
import { api, newIdempotencyKey, type Source } from '../api'
import { SourceStatusBadge } from '../components/badges'
import { LecturePanel } from '../components/lecture/LectureUpload'
import { EmptyState, ErrorBox, ExperimentalTag, Loading, Pill, ProgressBar, Spinner } from '../components/ui'
import { useViewer } from '../components/viewerContext'
import { useSources } from '../hooks/queries'
import { MAX_UPLOAD_BYTES, UPLOAD_ACCEPT, features } from '../lib/features'
import { LANGUAGE, SOURCE_KIND, SOURCE_TYPE, isIngesting, isLecture, isQueryable, qualityFlag } from '../lib/labels'
import { formatMs } from '../lib/mediaUpload'
import { t } from '../i18n'

export function SourcesPage() {
  const { workspaceId = '' } = useParams()
  const sources = useSources(workspaceId)
  const queryClient = useQueryClient()
  const [rejectedFiles, setRejectedFiles] = useState<string[]>([])
  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['sources', workspaceId] })
    queryClient.invalidateQueries({ queryKey: ['workspace', workspaceId] })
  }

  const upload = useMutation({
    mutationFn: async (files: File[]) => {
      const errors: string[] = []
      for (const file of files) {
        try {
          await api.uploadSource(workspaceId, file, newIdempotencyKey())
        } catch (error) {
          errors.push(`${file.name}: ${error instanceof Error ? error.message : String(error)}`)
        }
      }
      if (errors.length) throw new Error(errors.join(' · '))
    },
    onSettled: refresh,
  })
  const remote = useMutation({
    mutationFn: (input: { url: string } | { doi: string }) => api.addRemoteSource(workspaceId, input, newIdempotencyKey()),
    onSettled: refresh,
  })
  const remove = useMutation({ mutationFn: (id: string) => api.deleteSource(id), onSettled: refresh })

  // Kiểm tra sơ bộ phía client; server vẫn là nơi kiểm tra MIME thực và giới hạn trang.
  function onFiles(files: File[]) {
    const allowed = UPLOAD_ACCEPT.split(',')
    const ok: File[] = []
    const bad: string[] = []
    for (const file of files) {
      const extension = `.${file.name.split('.').pop()?.toLowerCase()}`
      if (!allowed.includes(extension)) bad.push(t('{0}: định dạng chưa hỗ trợ', { 0: file.name }))
      else if (file.size > MAX_UPLOAD_BYTES) bad.push(t('{0}: vượt 50 MB', { 0: file.name }))
      else ok.push(file)
    }
    setRejectedFiles(bad)
    if (ok.length) upload.mutate(ok)
  }

  const items = sources.data ?? []
  const ready = items.filter((s) => isQueryable(s.status)).length

  return (
    <div className="space-y-6">
      <div className={clsx('grid gap-4', features.remoteSources && 'md:grid-cols-2')}>
        <DropZone busy={upload.isPending} onFiles={onFiles} />
        {features.remoteSources && <RemoteForm busy={remote.isPending} onSubmit={(input) => remote.mutate(input)} />}
      </div>
      <LecturePanel workspaceId={workspaceId} onCreated={refresh} />
      {rejectedFiles.length > 0 && <ErrorBox error={new Error(rejectedFiles.join(' · '))} />}
      {upload.error && <ErrorBox error={upload.error} />}
      {remote.error && <ErrorBox error={remote.error} />}
      {remove.error && <ErrorBox error={remove.error} />}

      <section>
        <div className="mb-3 flex items-baseline justify-between gap-2">
          <h2 className="font-semibold">{t('Nguồn trong workspace')}</h2>
          {sources.data && (
            <span className="text-sm text-muted">
              {ready}/{items.length} {t('sẵn sàng')}
            </span>
          )}
        </div>
        {sources.isPending && <Loading />}
        {sources.error && <ErrorBox error={sources.error} onRetry={() => sources.refetch()} />}
        {sources.data?.length === 0 && (
          <EmptyState icon={<FileUp className="size-8" />} title={t('Chưa có nguồn nào')}>
            {t('Tải lên PDF có lớp văn bản, Markdown hoặc TXT, hoặc thêm một video bài giảng để bắt đầu.')}
          </EmptyState>
        )}
        <ul className="space-y-2">
          {items.map((source) => (
            <SourceRow
              key={source.id}
              source={source}
              all={items}
              deleting={remove.isPending && remove.variables === source.id}
              onDelete={() => {
                if (confirm(t('Xóa nguồn {0} — “{1}”?\n\nNguồn bị ẩn ngay khỏi truy xuất; dữ liệu được xóa bởi job nền. Các lượt chạy cũ giữ tham chiếu nhưng ẩn nội dung.', { 0: source.alias, 1: source.title }))) {
                  remove.mutate(source.id)
                }
              }}
            />
          ))}
        </ul>
      </section>
    </div>
  )
}

function SourceRow({ source, all, deleting, onDelete }: { source: Source; all: Source[]; deleting: boolean; onDelete: () => void }) {
  const { openSource } = useViewer()
  const sameGroup = all.filter((s) => s.id !== source.id && s.origin_group_id === source.origin_group_id)
  const tombstoned = !!source.deleted_at
  return (
    <li className={clsx('card flex flex-col gap-3 p-4 sm:flex-row sm:items-center', tombstoned && 'opacity-60')}>
      <Pill className="w-fit shrink-0 bg-accent-soft text-accent">
        {isLecture(source.kind) && <Video className="size-3" aria-hidden />}
        {source.alias}
      </Pill>
      <div className="min-w-0 flex-1">
        <p className="truncate font-medium" title={source.title}>
          {source.title}
        </p>
        <p className="mt-0.5 flex flex-wrap gap-x-2 text-sm text-muted">
          <span>{SOURCE_KIND[source.kind]}</span>
          {/* Loại nguồn (bình duyệt/preprint/web…) không có nghĩa với bài giảng tự tải lên. */}
          {!isLecture(source.kind) && (
            <>
              <span>·</span>
              <span>{SOURCE_TYPE[source.source_type]}</span>
            </>
          )}
          {isLecture(source.kind) && (source.duration_ms || source.language) && (
            <>
              <span>·</span>
              <span>
                {source.duration_ms ? formatMs(source.duration_ms) : ''}
                {source.duration_ms && source.language ? ', ' : ''}
                {source.language && LANGUAGE[source.language]}
              </span>
            </>
          )}
          {source.revision && (
            <>
              <span>·</span>
              <span>
                {isLecture(source.kind) ? '' : source.revision.page_count === null ? t('không phân trang, ') : `${source.revision.page_count} trang, `}
                {source.revision.leaf_count} {t('đoạn lá')}
              </span>
              <span>·</span>
              <span>{t('revision')} {source.revision.revision_no}</span>
            </>
          )}
        </p>
        <p className="mt-0.5 text-xs text-muted">
          {t('Nhóm nguồn gốc')} <span className="font-mono">{source.origin_group_id}</span>
          {sameGroup.length > 0 && (
            <span className="text-contested" title={source.grouping_reason}>
              {' '}{t('· cùng nhóm với')} {sameGroup.map((s) => s.alias).join(', ')} {t('— không tính là đối chứng của nhau')}
            </span>
          )}
          {' '}{t('· độc lập:')} {source.independence_status === 'reviewed' ? t('đã đánh giá') : t('chưa xác định')}
          {source.grouping_reason && <span className="block">{t('Lý do nhóm:')} {source.grouping_reason}</span>}
        </p>
        {isLecture(source.kind) && source.coverage && <LectureCoverage source={source} />}
        {isIngesting(source.status) && (
          <div className="mt-2 max-w-sm">
            <ProgressBar value={source.progress} label={t('Tiến độ nạp {0}', { 0: source.alias })} />
          </div>
        )}
        {source.error && (
          <p className="mt-1 text-sm text-rejected">
            {source.error.message} <span className="font-mono text-xs opacity-80">({source.error.code})</span>
          </p>
        )}
        {tombstoned && <p className="mt-1 text-sm text-muted">{t('Đã ẩn khỏi truy xuất; job nền đang xóa vector, file và snapshot.')}</p>}
      </div>
      <div className="flex items-center gap-1">
        <SourceStatusBadge status={source.status} />
        <button
          className="btn btn-ghost p-2"
          disabled={!isQueryable(source.status) || !source.revision || tombstoned}
          onClick={() => openSource(source)}
          aria-label={t('Xem nguồn {0}', { 0: source.alias })}
        >
          <Eye className="size-4" />
        </button>
        <button className="btn btn-ghost p-2 text-rejected" onClick={onDelete} disabled={deleting || tombstoned} aria-label={t('Xóa nguồn {0}', { 0: source.alias })}>
          {deleting ? <Spinner /> : <Trash2 className="size-4" />}
        </button>
      </div>
    </li>
  )
}

function LectureCoverage({ source }: { source: Source }) {
  const c = source.coverage!
  const parts = [
    c.captions_used ? t('phụ đề') : c.audio_read ? t('chép lời tự động') : t('không có lời'),
    c.visual_read ? t('{0} khung hình, {1} vùng chữ', { 0: c.frames_selected ?? 0, 1: c.board_regions ?? 0 }) : t('không có hình'),
    c.segments_flagged ? t('{0}/{1} đoạn bị cảnh báo', { 0: c.segments_flagged, 1: c.segments }) : null,
  ].filter(Boolean)
  return (
    <p className="mt-0.5 text-xs text-muted">
      {t('Đã đọc:')} {parts.join(' · ')}
      {source.status === 'review_required' && <span className="text-contested"> {t('— nên mở và sửa các đoạn bị cảnh báo (')}{qualityFlag('low_confidence').toLowerCase()}…)</span>}
    </p>
  )
}

function DropZone({ busy, onFiles }: { busy: boolean; onFiles: (files: File[]) => void }) {
  const input = useRef<HTMLInputElement>(null)
  const [over, setOver] = useState(false)

  function onDrop(event: DragEvent) {
    event.preventDefault()
    setOver(false)
    const files = [...event.dataTransfer.files]
    if (files.length) onFiles(files)
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault()
        setOver(true)
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={clsx(
        'flex flex-col items-center justify-center gap-2 rounded-lg border-2 border-dashed p-6 text-center transition-colors',
        over ? 'border-accent bg-accent-soft' : 'border-line bg-surface',
      )}
    >
      {busy ? <Spinner className="size-6 text-accent" /> : <FileUp className="size-6 text-muted" />}
      <p className="font-medium">{t('Kéo thả tài liệu vào đây')}</p>
      <p className="text-sm text-muted">
        {features.remoteSources ? t('PDF có lớp văn bản, Markdown, TXT, DOCX, HTML') : t('PDF có lớp văn bản, Markdown, TXT')} {t('· tối đa 50 MB, 300 trang')}
      </p>
      <button type="button" className="btn btn-outline mt-1" onClick={() => input.current?.click()} disabled={busy}>
        {t('Chọn file')}
      </button>
      <input
        ref={input}
        type="file"
        multiple
        accept={UPLOAD_ACCEPT}
        className="hidden"
        onChange={(e) => {
          const files = [...(e.target.files ?? [])]
          if (files.length) onFiles(files)
          e.target.value = ''
        }}
      />
    </div>
  )
}

function RemoteForm({ busy, onSubmit }: { busy: boolean; onSubmit: (input: { url: string } | { doi: string }) => void }) {
  const [kind, setKind] = useState<'url' | 'doi'>('url')
  const [value, setValue] = useState('')

  function submit(event: FormEvent) {
    event.preventDefault()
    const trimmed = value.trim()
    if (!trimmed) return
    onSubmit(kind === 'url' ? { url: trimmed } : { doi: trimmed })
    setValue('')
  }

  return (
    <form onSubmit={submit} className="card flex flex-col gap-3 p-6">
      <div className="flex items-center gap-2">
        <Link2 className="size-5 text-muted" />
        <p className="font-medium">{t('Thêm từ web hoặc DOI')}</p>
        <ExperimentalTag />
      </div>
      <div className="inline-flex w-fit rounded-md border border-line p-0.5" role="radiogroup">
        {(['url', 'doi'] as const).map((k) => (
          <button
            key={k}
            type="button"
            role="radio"
            aria-checked={k === kind}
            onClick={() => setKind(k)}
            className={clsx('rounded px-3 py-1 text-sm', k === kind ? 'bg-accent text-accent-fg' : 'text-muted')}
          >
            {k === 'url' ? 'URL' : 'DOI / arXiv'}
          </button>
        ))}
      </div>
      <input
        className="input"
        value={value}
        onChange={(e) => setValue(e.target.value)}
        placeholder={kind === 'url' ? 'https://…' : t('10.18653/v1/… hoặc 2401.18059')}
        aria-label={kind === 'url' ? 'URL' : t('DOI hoặc arXiv ID')}
      />
      <p className="text-xs text-muted">{t('Trang web được lưu snapshot đã làm sạch; nội dung nguồn được coi là dữ liệu không tin cậy.')}</p>
      <button className="btn btn-primary w-fit" disabled={busy || !value.trim()}>
        {busy && <Spinner />} {t('Thêm nguồn')}
      </button>
    </form>
  )
}
