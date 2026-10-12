import { useMutation, useQueryClient } from '@tanstack/react-query'
import clsx from 'clsx'
import { BookText, MonitorPlay, Pause, Play, Video, X } from 'lucide-react'
import { useRef, useState, type FormEvent } from 'react'
import { api, type Language } from '../../api'
import { useWorkspace } from '../../hooks/queries'
import { MAX_MEDIA_BYTES } from '../../lib/features'
import { LANGUAGE } from '../../lib/labels'
import {
  CAPTION_ACCEPT,
  VIDEO_ACCEPT,
  cancelVideoUpload,
  formatBytes,
  uploadVideo,
  videoMime,
  type UploadProgress,
} from '../../lib/mediaUpload'
import { ErrorBox, Notice, ProgressBar, Spinner } from '../ui'
import { t } from '../../i18n'

/** Thêm bài giảng: video tải lên hoặc YouTube kèm phụ đề (LECTURE_ARCHITECTURE.md mục 3–4). */
export function LecturePanel({ workspaceId, onCreated }: { workspaceId: string; onCreated: () => void }) {
  const [tab, setTab] = useState<'video' | 'youtube'>('video')
  return (
    <section className="card flex flex-col gap-4 p-6" aria-labelledby="lecture-heading">
      <div className="flex flex-wrap items-center gap-3">
        <Video className="size-5 text-muted" aria-hidden />
        <h2 id="lecture-heading" className="font-medium">
          {t('Thêm bài giảng')}
        </h2>
        <div className="ml-auto inline-flex rounded-md border border-line p-0.5" role="tablist" aria-label={t('Loại bài giảng')}>
          {(
            [
              ['video', t('Video tải lên'), Video],
              ['youtube', 'YouTube', MonitorPlay],
            ] as const
          ).map(([key, label, Icon]) => (
            <button
              key={key}
              type="button"
              role="tab"
              aria-selected={tab === key}
              onClick={() => setTab(key)}
              className={clsx('inline-flex items-center gap-1.5 rounded px-3 py-1 text-sm', tab === key ? 'bg-accent text-accent-fg' : 'text-muted')}
            >
              <Icon className="size-3.5" aria-hidden /> {label}
            </button>
          ))}
        </div>
      </div>
      {tab === 'video' ? <VideoUploadForm workspaceId={workspaceId} onCreated={onCreated} /> : <YoutubeForm workspaceId={workspaceId} onCreated={onCreated} />}
      <GlossaryEditor workspaceId={workspaceId} />
    </section>
  )
}

function LanguageSelect({ value, onChange, disabled }: { value: Language; onChange: (value: Language) => void; disabled?: boolean }) {
  return (
    <label className="flex flex-col gap-1 text-sm">
      <span className="label">{t('Ngôn ngữ lời giảng')}</span>
      <select className="input" value={value} onChange={(e) => onChange(e.target.value as Language)} disabled={disabled}>
        {(['vi', 'en', 'auto'] as const).map((l) => (
          <option key={l} value={l}>
            {LANGUAGE[l]}
          </option>
        ))}
      </select>
    </label>
  )
}

function FileField({
  label,
  accept,
  file,
  onChange,
  disabled,
  hint,
}: {
  label: string
  accept: string
  file: File | null
  onChange: (file: File | null) => void
  disabled?: boolean
  hint?: string
}) {
  const input = useRef<HTMLInputElement>(null)
  return (
    <div className="flex flex-col gap-1 text-sm">
      <span className="label">{label}</span>
      <div className="flex items-center gap-2">
        <button type="button" className="btn btn-outline shrink-0" onClick={() => input.current?.click()} disabled={disabled}>
          {t('Chọn file')}
        </button>
        <span className={clsx('min-w-0 truncate', file ? 'text-fg' : 'text-muted')} title={file?.name}>
          {file ? `${file.name} · ${formatBytes(file.size)}` : t('Chưa chọn')}
        </span>
        {file && !disabled && (
          <button type="button" className="btn btn-ghost p-1" onClick={() => onChange(null)} aria-label={t('Bỏ chọn {0}', { 0: label })}>
            <X className="size-4" />
          </button>
        )}
      </div>
      {hint && <span className="text-xs text-muted">{hint}</span>}
      <input
        ref={input}
        type="file"
        accept={accept}
        className="hidden"
        aria-label={label}
        onChange={(e) => {
          onChange(e.target.files?.[0] ?? null)
          e.target.value = ''
        }}
      />
    </div>
  )
}

type Phase = { kind: 'idle' } | { kind: 'uploading' } | { kind: 'paused' } | { kind: 'done'; alias: string }

function VideoUploadForm({ workspaceId, onCreated }: { workspaceId: string; onCreated: () => void }) {
  const [file, setFile] = useState<File | null>(null)
  const [caption, setCaption] = useState<File | null>(null)
  const [language, setLanguage] = useState<Language>('vi')
  const [keepOriginal, setKeepOriginal] = useState(false)
  const [progress, setProgress] = useState<UploadProgress | null>(null)
  const [phase, setPhase] = useState<Phase>({ kind: 'idle' })
  const [error, setError] = useState<unknown>(null)
  const controller = useRef<AbortController | null>(null)
  const busy = phase.kind === 'uploading'

  function choose(next: File | null) {
    setError(null)
    setProgress(null)
    setPhase({ kind: 'idle' })
    if (next && !videoMime(next)) {
      setError(new Error(t('{0}: chỉ nhận video MP4/WebM.', { 0: next.name })))
      return
    }
    if (next && next.size > MAX_MEDIA_BYTES) {
      setError(new Error(t('{0}: vượt giới hạn {1}.', { 0: next.name, 1: formatBytes(MAX_MEDIA_BYTES) })))
      return
    }
    setFile(next)
  }

  async function start(event?: FormEvent) {
    event?.preventDefault()
    if (!file || busy) return
    setError(null)
    setPhase({ kind: 'uploading' })
    controller.current = new AbortController()
    try {
      const source = await uploadVideo(api, {
        workspaceId,
        file,
        language,
        keepOriginal,
        caption,
        onProgress: setProgress,
        signal: controller.current.signal,
      })
      setPhase({ kind: 'done', alias: source.alias })
      setFile(null)
      setCaption(null)
      setProgress(null)
      onCreated()
    } catch (caught) {
      if (caught instanceof DOMException && caught.name === 'AbortError') {
        setPhase({ kind: 'paused' })
      } else {
        setError(caught)
        setPhase({ kind: 'paused' })
      }
    }
  }

  async function cancel() {
    controller.current?.abort()
    if (file && progress) {
      try {
        await cancelVideoUpload(api, workspaceId, file, progress.sessionId)
      } catch (caught) {
        setError(caught)
      }
    }
    setProgress(null)
    setPhase({ kind: 'idle' })
  }

  const fraction = progress ? progress.sentBytes / Math.max(1, progress.totalBytes) : 0
  return (
    <form onSubmit={start} className="flex flex-col gap-4">
      <div className="grid gap-4 sm:grid-cols-2">
        <FileField
          label={t('Video bài giảng')}
          accept={VIDEO_ACCEPT}
          file={file}
          onChange={choose}
          disabled={busy}
          hint={t('MP4/WebM, tối đa {0}. Tải theo từng phần — mất mạng thì chọn lại đúng file để tải tiếp.', { 0: formatBytes(MAX_MEDIA_BYTES) })}
        />
        <FileField
          label={t('Phụ đề (tùy chọn)')}
          accept={CAPTION_ACCEPT}
          file={caption}
          onChange={setCaption}
          disabled={busy}
          hint={t('SRT/VTT bạn có quyền dùng. Có phụ đề thì bỏ qua bước chép lời tự động.')}
        />
        <LanguageSelect value={language} onChange={setLanguage} disabled={busy} />
        <label className="flex items-start gap-2 self-end text-sm">
          <input type="checkbox" className="mt-1" checked={keepOriginal} onChange={(e) => setKeepOriginal(e.target.checked)} disabled={busy} />
          <span>
            {t('Giữ video gốc')}
            <span className="block text-xs text-muted">{t('Mặc định chỉ giữ âm thanh và ảnh bảng/slide làm bằng chứng; video gốc bị xóa sau khi xử lý.')}</span>
          </span>
        </label>
      </div>

      {progress && (
        <div className="space-y-1.5">
          <ProgressBar value={fraction} label={t('Tiến độ tải video')} />
          <p className="text-xs text-muted">
            {formatBytes(progress.sentBytes)} / {formatBytes(progress.totalBytes)} {t('· phần')} {progress.partsDone}/{progress.partCount}
            {progress.resumed && t(' · tiếp tục phiên trước')}
            {phase.kind === 'paused' && t(' · đã tạm dừng')}
          </p>
        </div>
      )}
      {error !== null && <ErrorBox error={error} />}
      {phase.kind === 'done' && (
        <Notice title={t('Đã tạo nguồn {0}', { 0: phase.alias })}>
          {t('Đang chép lời và đọc bảng; bài giảng dài có thể mất vài phút. Theo dõi tiến độ trong danh sách nguồn bên dưới.')}
        </Notice>
      )}

      <div className="flex flex-wrap gap-2">
        {busy ? (
          <button type="button" className="btn btn-outline" onClick={() => controller.current?.abort()}>
            <Pause className="size-4" /> {t('Tạm dừng')}
          </button>
        ) : (
          <button className="btn btn-primary" disabled={!file}>
            {phase.kind === 'paused' ? <Play className="size-4" /> : <Video className="size-4" />}
            {phase.kind === 'paused' ? t('Tiếp tục tải') : t('Tải lên và xử lý')}
          </button>
        )}
        {progress && (
          <button type="button" className="btn btn-ghost text-rejected" onClick={cancel}>
            {t('Hủy và xóa phần đã tải')}
          </button>
        )}
        {busy && <Spinner className="self-center text-accent" />}
      </div>
    </form>
  )
}

function YoutubeForm({ workspaceId, onCreated }: { workspaceId: string; onCreated: () => void }) {
  const [url, setUrl] = useState('')
  const [title, setTitle] = useState('')
  const [caption, setCaption] = useState<File | null>(null)
  const [language, setLanguage] = useState<Language>('vi')
  const add = useMutation({
    mutationFn: () => api.addYoutube(workspaceId, { url: url.trim(), title: title.trim() || undefined, language, caption: caption! }),
    onSuccess: () => {
      setUrl('')
      setTitle('')
      setCaption(null)
      onCreated()
    },
  })
  return (
    <form
      className="flex flex-col gap-4"
      onSubmit={(e) => {
        e.preventDefault()
        if (url.trim() && caption) add.mutate()
      }}
    >
      <Notice>
        {t('Chưa kết nối YouTube API, nên hệ thống không tự tải video hay phụ đề. Hãy kèm file phụ đề SRT/VTT bạn được phép dùng; nguồn sẽ chỉ có lời (không có hình bảng) và được đánh dấu “Sẵn sàng (giới hạn)”.')}
      </Notice>
      <div className="grid gap-4 sm:grid-cols-2">
        <label className="flex flex-col gap-1 text-sm">
          <span className="label">{t('Link YouTube')}</span>
          <input className="input" value={url} onChange={(e) => setUrl(e.target.value)} placeholder={t('https://www.youtube.com/watch?v=…')} />
        </label>
        <label className="flex flex-col gap-1 text-sm">
          <span className="label">{t('Tiêu đề (tùy chọn)')}</span>
          <input className="input" value={title} onChange={(e) => setTitle(e.target.value)} maxLength={300} />
        </label>
        <FileField label={t('Phụ đề (bắt buộc)')} accept={CAPTION_ACCEPT} file={caption} onChange={setCaption} disabled={add.isPending} />
        <LanguageSelect value={language} onChange={setLanguage} disabled={add.isPending} />
      </div>
      {add.error && <ErrorBox error={add.error} />}
      <button className="btn btn-primary w-fit" disabled={add.isPending || !url.trim() || !caption}>
        {add.isPending && <Spinner />} {t('Thêm bài giảng YouTube')}
      </button>
    </form>
  )
}

const GLOSSARY_MAX_TERMS = 40
const GLOSSARY_MAX_CHARS = 400

function GlossaryEditor({ workspaceId }: { workspaceId: string }) {
  const workspace = useWorkspace(workspaceId)
  const queryClient = useQueryClient()
  const saved = workspace.data?.asr_glossary ?? []
  const [draft, setDraft] = useState<string | null>(null)
  const text = draft ?? saved.join('\n')
  const terms = text
    .split(/[\n,]/)
    .map((t) => t.trim())
    .filter(Boolean)
  const chars = terms.join(', ').length
  const save = useMutation({
    mutationFn: () => api.setAsrGlossary(workspaceId, terms),
    onSuccess: () => {
      setDraft(null)
      queryClient.invalidateQueries({ queryKey: ['workspace', workspaceId] })
    },
  })
  const over = terms.length > GLOSSARY_MAX_TERMS || chars > GLOSSARY_MAX_CHARS
  return (
    <details className="rounded-md border border-line bg-surface-2 px-3 py-2 text-sm">
      <summary className="flex cursor-pointer items-center gap-2 font-medium">
        <BookText className="size-4 text-muted" aria-hidden />
        {t('Thuật ngữ cho chép lời tự động')}
        <span className="font-normal text-muted">({saved.length ? t('{0} thuật ngữ', { 0: saved.length }) : t('chưa có')})</span>
      </summary>
      <div className="mt-3 space-y-2">
        <p className="text-xs text-muted">
          {t('Tên riêng, thuật ngữ tiếng Anh hay bị chép sai (vd. “SBERT”, “RAPTOR”). Đo trên bài giảng mẫu: lỗi chép giảm từ 5,8 % xuống 3,6 %. Chỉ áp dụng cho video tải lên')} <strong>{t('sau khi lưu')}</strong>{t('; giữ danh sách ngắn — thuật ngữ thừa có thể bị chèn vào đoạn im lặng.')}
        </p>
        <textarea
          className="input min-h-24 font-mono text-xs"
          value={text}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={'Agreement Ranking\ncross-encoder\nSBERT'}
          aria-label={t('Thuật ngữ, mỗi dòng một mục')}
        />
        <div className="flex flex-wrap items-center gap-3">
          <span className={clsx('text-xs', over ? 'text-rejected' : 'text-muted')}>
            {terms.length}/{GLOSSARY_MAX_TERMS} {t('thuật ngữ ·')} {chars}/{GLOSSARY_MAX_CHARS} {t('ký tự')}
          </span>
          <button type="button" className="btn btn-outline ml-auto" disabled={draft === null || over || save.isPending} onClick={() => save.mutate()}>
            {save.isPending && <Spinner />} {t('Lưu thuật ngữ')}
          </button>
        </div>
        {save.error && <ErrorBox error={save.error} />}
      </div>
    </details>
  )
}
