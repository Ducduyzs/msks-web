// Tải video theo phiên nhiều phần (LECTURE_ARCHITECTURE.md mục 4): API không nhận bytes video; trình duyệt PUT
// từng phần (≤ part_bytes) vào URL ký sẵn rồi gọi `complete`. Phiên được nhớ theo (workspace, tên, kích thước,
// lastModified) để chọn lại đúng file sau khi mất mạng/tải lại trang thì chỉ tải tiếp phần còn thiếu.
import { ApiError, newIdempotencyKey, type Api, type Language, type MediaUploadSession, type Source } from '../api'
import { t } from '../i18n'

export const VIDEO_ACCEPT = '.mp4,.m4v,.webm'
export const CAPTION_ACCEPT = '.srt,.vtt'
const VIDEO_MIME: Record<string, string> = { mp4: 'video/mp4', m4v: 'video/mp4', webm: 'video/webm' }
const PART_ATTEMPTS = 3

export interface UploadProgress {
  sessionId: string
  sentBytes: number
  totalBytes: number
  partsDone: number
  partCount: number
  /** Tiếp tục một phiên cũ thay vì tạo mới. */
  resumed: boolean
}

export interface VideoUploadOptions {
  workspaceId: string
  file: File
  language: Language
  keepOriginal: boolean
  caption?: File | null
  onProgress?: (progress: UploadProgress) => void
  signal?: AbortSignal
}

export function videoMime(file: File) {
  const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
  return VIDEO_MIME[extension] ?? null
}

export function resumeKey(workspaceId: string, file: File) {
  return `msks.upload.${workspaceId}.${file.size}.${file.lastModified}.${file.name}`
}

// localStorage có thể bị chặn (chế độ riêng tư): mất khả năng resume, không mất khả năng tải.
function readSaved(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

function writeSaved(key: string, sessionId: string | null) {
  try {
    if (sessionId) localStorage.setItem(key, sessionId)
    else localStorage.removeItem(key)
  } catch {
    /* bỏ qua */
  }
}

async function openSession(api: Api, options: VideoUploadOptions, mime: string): Promise<{ session: MediaUploadSession; resumed: boolean }> {
  const key = resumeKey(options.workspaceId, options.file)
  const saved = readSaved(key)
  if (saved) {
    try {
      const session = await api.getMediaSession(saved)
      if (session.status === 'open' || session.status === 'completed') return { session, resumed: true }
    } catch (error) {
      if (!(error instanceof ApiError) || error.status !== 404) throw error
    }
    writeSaved(key, null)
  }
  const session = await api.createMediaSession(
    options.workspaceId,
    { filename: options.file.name, size_bytes: options.file.size, mime, language: options.language, keep_original: options.keepOriginal },
    newIdempotencyKey(),
  )
  writeSaved(key, session.id)
  return { session, resumed: false }
}

/** Tải video và tạo nguồn bài giảng. Bị hủy bằng `signal` thì phiên được giữ để tiếp tục lần sau. */
export async function uploadVideo(api: Api, options: VideoUploadOptions): Promise<Source> {
  const { file, signal } = options
  const mime = videoMime(file)
  if (!mime) throw new ApiError(415, { code: 'unsupported_media_type', message: t('Chỉ nhận video MP4/WebM.'), retryable: false })
  const key = resumeKey(options.workspaceId, file)
  let { session, resumed } = await openSession(api, options, mime)

  if (session.status === 'completed') {
    const source = await api.completeMediaSession(session.id) // idempotent: trả lại nguồn đã tạo
    writeSaved(key, null)
    return source
  }
  if (session.parts.some((p) => p.error)) {
    throw new ApiError(409, {
      code: 'part_size_mismatch',
      message: t('Một phần đã tải lên sai kích thước và không ghi đè được. Hãy hủy phiên rồi tải lại từ đầu.'),
      retryable: false,
    })
  }
  if (options.caption && !session.caption) session = await api.attachCaption(session.id, options.caption)

  const done = new Set(session.parts.filter((p) => p.uploaded).map((p) => p.index))
  const sizeOf = (index: number) => session.parts[index].size
  let completedBytes = [...done].reduce((sum, i) => sum + sizeOf(i), 0)
  const report = (inFlight = 0) =>
    options.onProgress?.({
      sessionId: session.id,
      sentBytes: completedBytes + inFlight,
      totalBytes: session.expected_bytes,
      partsDone: done.size,
      partCount: session.part_count,
      resumed,
    })
  report()

  for (const part of session.parts) {
    if (done.has(part.index)) continue
    const offset = part.index * session.part_bytes
    const blob = file.slice(offset, offset + part.size)
    let url = part.upload_url
    for (let attempt = 1; ; attempt++) {
      signal?.throwIfAborted()
      try {
        if (!url) throw new ApiError(409, { code: 'missing_upload_url', message: t('Thiếu URL tải lên.'), retryable: true })
        await api.uploadPart(url, blob, (loaded) => report(loaded), signal)
        break
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') throw error
        if (!(error instanceof ApiError) || !error.retryable || attempt >= PART_ATTEMPTS) throw error
        // URL ký có hạn; lấy trạng thái mới — phần có thể đã lên đủ dù kết nối rớt ở cuối.
        const fresh = await api.getMediaSession(session.id)
        const current = fresh.parts[part.index]
        if (current.uploaded) break
        if (current.error) throw new ApiError(409, { code: 'part_size_mismatch', message: t('Phần video tải lên bị lỗi kích thước; hãy hủy và tải lại.'), retryable: false })
        url = current.upload_url
      }
    }
    done.add(part.index)
    completedBytes += part.size
    report()
  }

  const source = await api.completeMediaSession(session.id)
  writeSaved(key, null)
  return source
}

/** Hủy phiên đang dở (xóa các phần đã tải) và quên trạng thái resume. */
export async function cancelVideoUpload(api: Api, workspaceId: string, file: File, sessionId: string) {
  writeSaved(resumeKey(workspaceId, file), null)
  await api.cancelMediaSession(sessionId)
}

export function formatBytes(bytes: number) {
  if (bytes < 1024 * 1024) return `${Math.max(1, Math.round(bytes / 1024))} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(2)} GB`
}

/** mm:ss hoặc h:mm:ss. */
export function formatMs(ms: number | null | undefined) {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return '—'
  const total = Math.max(0, Math.floor(ms / 1000))
  const h = Math.floor(total / 3600)
  const m = Math.floor((total % 3600) / 60)
  const s = String(total % 60).padStart(2, '0')
  return h ? `${h}:${String(m).padStart(2, '0')}:${s}` : `${m}:${s}`
}
