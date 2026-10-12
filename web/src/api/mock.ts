// API giả lập trong bộ nhớ: cùng interface với HttpApi, dùng khi chưa có backend.
// Mô phỏng: vòng đời nạp nguồn + job, run QUEUED→RUNNING→terminal, SSE có seq và
// phát lại, hủy run, tombstone nguồn, answer_status và chỉ số mục 8.4.
//
// Từ khóa thử trạng thái trong câu hỏi: [invalid] → model_output_invalid,
// [verifier-down] → verification_unavailable, [fail] → run FAILED.
// Tên file chứa "scan" → nạp thất bại. Tiêu đề mục dàn ý chứa [fail] → run PARTIAL.
import type { Api, StreamHandle } from './client'
import { ApiError } from './client'
import {
  REVIEWED_INDEPENDENT,
  SEED_CLAIMS,
  SEED_OUTLINE,
  SEED_REJECTED,
  SEED_SOURCES,
  TEMPLATE_EMPTY,
  TEMPLATE_FAILED,
  TEMPLATE_LEAD,
  type SeedClaim,
} from './mockData'
import type {
  AnswerStatus,
  ApiErrorBody,
  Budget,
  ClaimResult,
  ContextBlock,
  Modality,
  EvidenceRef,
  ExportFormat,
  Job,
  Language,
  LocatorItem,
  MediaCapabilities,
  MediaSessionRequest,
  MediaUploadSession,
  MediaUrl,
  OutlineRequest,
  Page,
  PageSpan,
  Provenance,
  QaRequest,
  QueryType,
  Readiness,
  RejectedClaim,
  ReportSection,
  Run,
  RunAccepted,
  RunEvent,
  RunMetrics,
  RunStage,
  RunStatus,
  RunSummary,
  Source,
  SourceContent,
  SourceKind,
  SourceStatus,
  SourceTranslation,
  TranslationDetail,
  TranslationLanguage,
  SynthesisRequest,
  Timeline as MediaTimeline,
  TranscriptEdit,
  Workspace,
} from './types'
import { utf16ToCp } from '../lib/codepoints'
import {
  LECTURE_DURATION_MS,
  buildTimeline,
  frameDataUrl,
  lectureContent,
  lectureCoverage,
  parseCaptions,
  sampleLecture,
  type LectureData,
} from './mockLecture'

// ------------------------------------------------------------------ helpers

interface StoredSource {
  source: Source
  content: SourceContent | null
  purged: boolean
  /** Bài giảng: dữ liệu trích xuất theo từng parse revision (sửa bản chép tạo revision mới). */
  lecture?: { revisions: Map<string, MediaTimeline>; captions: boolean }
}

interface StoredSession {
  session: MediaUploadSession
  uploaded: Set<number>
  captionText?: string
  sourceId?: string
}

const MEDIA_PART_BYTES = 45 * 1024 * 1024
const MAX_MEDIA_BYTES = 400 * 1024 * 1024
const REVIEW_RATIO = 0.5
const LECTURE_KINDS: SourceKind[] = ['video', 'youtube', 'drive']
const isLectureKind = (kind: SourceKind) => LECTURE_KINDS.includes(kind)
const QUERYABLE: SourceStatus[] = ['ready', 'ready_limited', 'review_required']

type PlannedEvent = { at: number; event: RunEvent }

interface Subscriber {
  onEvent: (event: RunEvent) => void
  timers: ReturnType<typeof setTimeout>[]
}

interface StoredRun {
  final: Run
  planned: PlannedEvent[]
  startedAt: number
  cancelledAt?: number
  subscribers: Set<Subscriber>
}

type DistributiveOmit<T, K extends keyof never> = T extends unknown ? Omit<T, K> : never
type RejectedWithSupport = RejectedClaim & { support: number | null }

const STAGE_LABELS: Record<RunStage, string> = {
  queued: 'Đã xếp hàng',
  classify: 'Phân loại truy vấn',
  retrieve: 'Sinh ứng viên (dense + sparse → RRF)',
  rank: 'Agreement Ranking (cross-encoder + SBERT)',
  pack: 'Đóng gói context theo ngân sách',
  generate: 'LLM sinh claim',
  verify: 'Kiểm chứng NLI trên evidence hiển thị',
  cross_check: 'Đối chứng chéo nguồn',
  outline: 'Chốt dàn ý',
  subquestion: 'Trả lời câu hỏi con',
  compose: 'Ghép mục báo cáo theo template',
}

const BLOCKS_PER_BUDGET: Record<Budget, number> = { 512: 3, 1024: 4, 2048: 8 }
const PAGE_SIZE = 10
const MAX_BYTES = 50 * 1024 * 1024

let counter = 0
const uid = (prefix: string) => `${prefix}_${Date.now().toString(36)}${(counter++).toString(36)}`
const nowIso = () => new Date().toISOString()
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms))
const clone = <T>(value: T): T => structuredClone(value)
const err = (status: number, code: string, message: string, retryable = false) =>
  new ApiError(status, { code, message, retryable, trace_id: uid('trace') })

function rate(numerator: number, denominator: number) {
  return denominator > 0 ? numerator / denominator : null
}

function buildContent(sourceId: string, parseRevisionId: string, pages: string[][], paginated: boolean): SourceContent {
  let text = ''
  const spans: PageSpan[] = []
  pages.forEach((paragraphs, index) => {
    if (text) text += '\n\n'
    const start = text.length
    text += paragraphs.join('\n\n')
    spans.push({ page: index + 1, char_start: utf16ToCp(text, start), char_end: utf16ToCp(text, text.length) })
  })
  return { source_id: sourceId, parse_revision_id: parseRevisionId, text, pages: paginated ? spans : null }
}

// Bản rút gọn của edahr.pipeline.classify_query.
function classifyQuery(query: string): QueryType {
  const q = ` ${query.toLowerCase()} `
  const has = (terms: string[]) => terms.some((term) => q.includes(term))
  if (has(['compare', 'versus', ' vs ', 'difference', 'better', 'so sánh', 'khác nhau', 'hơn hay', 'so với'])) {
    return 'comparative_multi_hop'
  }
  if (has(['overall', 'summarize', 'key findings', 'toàn bộ', 'tổng quan', 'kết luận', 'đóng góp chính'])) {
    return 'global_synthesis'
  }
  if (has(['why', 'how', 'explain', 'tại sao', 'như thế nào', 'giải thích', 'vì sao'])) return 'explanatory'
  return 'factoid'
}

class Timeline {
  readonly events: PlannedEvent[] = []
  private t = 0
  private seq = 0

  push(event: DistributiveOmit<RunEvent, 'seq'>, gap: number) {
    this.t += gap
    this.events.push({ at: this.t, event: { ...event, seq: ++this.seq } as RunEvent })
  }

  stage(stage: RunStage, gap = 450, label?: string) {
    this.push({ type: 'stage', stage, label: label ?? STAGE_LABELS[stage] }, gap)
  }
}

// --------------------------------------------------------------------- api

export class MockApi implements Api {
  readonly mode = 'mock' as const
  private workspaces = new Map<string, Workspace>()
  private sources = new Map<string, StoredSource>()
  private runs = new Map<string, StoredRun>()
  private jobs = new Map<string, Job>()
  private idempotent = new Map<string, Promise<unknown>>()
  private sessions = new Map<string, StoredSession>()
  private translations = new Map<string, TranslationDetail>()

  constructor() {
    const workspace: Workspace = {
      id: 'ws_demo',
      name: 'Demo: Agreement Ranking và RAPTOR',
      created_at: '2026-10-05T09:00:00.000Z',
      source_count: 0,
      run_count: 0,
    }
    this.workspaces.set(workspace.id, workspace)
    for (const seed of SEED_SOURCES) {
      const id = `src_${seed.alias.toLowerCase()}`
      const parseRevisionId = `pr_${seed.alias.toLowerCase()}_1`
      const source: Source = {
        id,
        workspace_id: workspace.id,
        alias: seed.alias,
        kind: seed.kind,
        title: seed.title,
        authors: seed.authors,
        url: seed.url,
        doi: seed.doi,
        published_at: seed.published_at,
        source_type: seed.source_type,
        origin_group_id: seed.origin_group_id,
        independence_status: REVIEWED_INDEPENDENT.flat().includes(seed.origin_group_id) ? 'reviewed' : 'unknown',
        grouping_version: 1,
        grouping_reason: seed.grouping_reason,
        status: seed.failed ? 'failed' : 'ready',
        progress: seed.failed ? 0.15 : 1,
        created_at: workspace.created_at,
      }
      if (seed.failed) {
        source.error = { ...seed.failed, retryable: false, trace_id: 'trace_seed' }
      } else {
        source.revision = {
          document_revision_id: `dr_${seed.alias.toLowerCase()}_1`,
          revision_no: 1,
          parse_revision_id: parseRevisionId,
          page_count: seed.paginated ? seed.pages.length : null,
          leaf_count: seed.pages.flat().length,
        }
      }
      this.sources.set(id, {
        source,
        content: seed.failed ? null : buildContent(id, parseRevisionId, seed.pages, seed.paginated),
        purged: false,
      })
    }
    // Bài giảng ở workspace riêng: một run chỉ dùng một model profile, nên trộn bài giảng với tài liệu tiếng Anh
    // khiến phạm vi "tất cả nguồn" bị từ chối (422 profile_index_missing) — giống backend.
    const course: Workspace = {
      id: 'ws_lecture',
      name: 'Demo: Bài giảng tiếng Việt',
      created_at: '2026-10-05T09:30:00.000Z',
      source_count: 0,
      run_count: 0,
      asr_glossary: ['Agreement Ranking', 'cross-encoder', 'SBERT', 'reciprocal rank fusion', 'RAPTOR', 'PeerQA'],
    }
    this.workspaces.set(course.id, course)
    this.createLecture(course.id, { kind: 'video', title: 'Bài giảng: Agreement Ranking (mẫu, tiếng Việt)', language: 'vi' }, true)
  }

  private once<T>(key: string, create: () => Promise<T>): Promise<T> {
    const existing = this.idempotent.get(key)
    if (existing) return existing as Promise<T>
    const promise = create()
    this.idempotent.set(key, promise)
    promise.catch(() => this.idempotent.delete(key))
    return promise
  }

  async getReadiness(): Promise<Readiness> {
    await wait(80)
    return {
      ready: true,
      components: { postgres: 'ok', redis: 'ok', qdrant: 'ok', gpu: 'ok', llm: 'ok' },
      llm_provider: null,
      calibrated: false,
    }
  }

  // ---------------------------------------------------------------- workspace

  private withCounts(workspace: Workspace): Workspace {
    return {
      ...workspace,
      source_count: this.visibleSources(workspace.id).length,
      run_count: [...this.runs.values()].filter((r) => r.final.workspace_id === workspace.id).length,
    }
  }

  private requireWorkspace(id: string) {
    const workspace = this.workspaces.get(id)
    if (!workspace) throw err(404, 'workspace_not_found', 'Không tìm thấy workspace.')
    return workspace
  }

  async listWorkspaces() {
    await wait(150)
    return [...this.workspaces.values()].map((w) => this.withCounts(w))
  }

  async createWorkspace(name: string) {
    await wait(150)
    if (!name.trim()) throw err(422, 'invalid_name', 'Tên workspace không được để trống.')
    const workspace: Workspace = { id: uid('ws'), name: name.trim(), created_at: nowIso(), source_count: 0, run_count: 0 }
    this.workspaces.set(workspace.id, workspace)
    return clone(workspace)
  }

  async getWorkspace(id: string) {
    await wait(100)
    return this.withCounts(this.requireWorkspace(id))
  }

  // ------------------------------------------------------------------ sources

  private visibleSources(workspaceId: string) {
    return [...this.sources.values()].filter((s) => s.source.workspace_id === workspaceId && !s.purged)
  }

  async listSources(workspaceId: string, cursor?: string): Promise<Page<Source>> {
    await wait(120)
    this.requireWorkspace(workspaceId)
    const all = this.visibleSources(workspaceId).map((s) => clone(s.source))
    const start = cursor ? Number(cursor) : 0
    const size = 50
    return { items: all.slice(start, start + size), next_cursor: start + size < all.length ? String(start + size) : null }
  }

  private nextAlias(workspaceId: string) {
    const used = [...this.sources.values()]
      .filter((s) => s.source.workspace_id === workspaceId)
      .map((s) => Number(s.source.alias.slice(1)))
    return `S${Math.max(0, ...used) + 1}`
  }

  private startIngest(workspaceId: string, partial: Pick<Source, 'kind' | 'title' | 'url' | 'doi'>, text: string, failure?: ApiErrorBody) {
    const id = uid('src')
    const jobId = uid('job')
    const paginated = partial.kind === 'pdf' || partial.kind === 'docx'
    const paragraphs = text.split(/\n{2,}/).map((p) => p.trim()).filter(Boolean)
    const stored: StoredSource = {
      content: null,
      purged: false,
      source: {
        id,
        workspace_id: workspaceId,
        alias: this.nextAlias(workspaceId),
        authors: [],
        source_type: partial.kind === 'html' ? 'web' : partial.kind === 'doi' ? 'peer_reviewed' : 'user_note',
        origin_group_id: `g-${id}`,
        independence_status: 'unknown',
        grouping_version: 1,
        status: 'uploaded',
        progress: 0.05,
        job_id: jobId,
        created_at: nowIso(),
        ...partial,
      },
    }
    this.sources.set(id, stored)
    this.jobs.set(jobId, { id: jobId, kind: 'ingest', target_id: id, state: 'queued', retry_count: 0 })
    const steps: [number, SourceStatus, number][] = [
      [600, 'parsing', 0.15],
      [1300, 'chunking', 0.35],
      [2000, 'embedding', 0.6],
      [2800, 'publishing', 0.85],
      [3400, 'ready', 1],
    ]
    for (const [at, status, progress] of steps) {
      setTimeout(() => {
        const current = this.sources.get(id)
        const job = this.jobs.get(jobId)
        if (!current || !job || current.source.status === 'failed' || current.source.deleted_at) return
        if (failure && status === 'chunking') {
          current.source.status = 'failed'
          current.source.error = failure
          Object.assign(job, { state: 'failed', error: failure })
          return
        }
        current.source.status = status
        current.source.progress = progress
        Object.assign(job, { state: status === 'ready' ? 'succeeded' : 'running', stage: status })
        if (status === 'ready') {
          const parseRevisionId = uid('pr')
          current.content = buildContent(id, parseRevisionId, [paragraphs], paginated)
          current.source.revision = {
            document_revision_id: uid('dr'),
            revision_no: 1,
            parse_revision_id: parseRevisionId,
            page_count: paginated ? 1 : null,
            leaf_count: Math.max(1, paragraphs.length),
          }
        }
      }, at)
    }
    return clone(stored.source)
  }

  uploadSource(workspaceId: string, file: File, idempotencyKey: string) {
    return this.once(`upload:${idempotencyKey}`, async () => {
      await wait(300)
      this.requireWorkspace(workspaceId)
      const extension = file.name.split('.').pop()?.toLowerCase() ?? ''
      const kinds: Record<string, SourceKind> = {
        pdf: 'pdf', docx: 'docx', md: 'markdown', markdown: 'markdown', txt: 'txt', html: 'html', htm: 'html',
      }
      const kind = kinds[extension]
      if (!kind) throw err(415, 'unsupported_media_type', `Định dạng .${extension} chưa được hỗ trợ.`)
      if (file.size > MAX_BYTES) throw err(413, 'file_too_large', 'File vượt giới hạn 50 MB.')
      const text = ['md', 'markdown', 'txt'].includes(extension)
        ? await file.text()
        : `Nội dung mẫu cho ${file.name}.\n\nChế độ mock không parse ${extension.toUpperCase()}; backend dùng Docling.`
      const failure = file.name.toLowerCase().includes('scan')
        ? { code: 'pdf_no_text_layer', message: 'PDF không có lớp văn bản (bản scan). OCR chưa được hỗ trợ trong MVP.', retryable: false, trace_id: uid('trace') }
        : undefined
      return this.startIngest(workspaceId, { kind, title: file.name.replace(/\.[^.]+$/, '') }, text, failure)
    })
  }

  addRemoteSource(workspaceId: string, input: { url: string } | { doi: string }, idempotencyKey: string) {
    return this.once(`remote:${idempotencyKey}`, async () => {
      await wait(300)
      this.requireWorkspace(workspaceId)
      if ('url' in input) {
        let host: string
        try {
          const url = new URL(input.url)
          if (!['http:', 'https:'].includes(url.protocol)) throw new Error()
          host = url.host
        } catch {
          throw err(422, 'invalid_url', 'URL không hợp lệ (chỉ hỗ trợ http/https).')
        }
        return this.startIngest(workspaceId, { kind: 'html', title: host, url: input.url },
          `Nội dung mẫu lấy từ ${input.url}.\n\nBackend tải trang qua worker có giới hạn egress và trích nội dung chính.`)
      }
      if (!/^(10\.\d{4,}\/\S+|\d{4}\.\d{4,5}(v\d+)?)$/.test(input.doi.trim())) {
        throw err(422, 'invalid_doi', 'DOI hoặc arXiv ID không hợp lệ.')
      }
      return this.startIngest(workspaceId, { kind: 'doi', title: `DOI ${input.doi}`, doi: input.doi },
        `Nội dung mẫu cho ${input.doi}.\n\nBackend tra Crossref/arXiv và chỉ tải bản có giấy phép mở.`)
    })
  }

  async deleteSource(sourceId: string) {
    await wait(150)
    const stored = this.sources.get(sourceId)
    if (!stored || stored.purged) throw err(404, 'source_not_found', 'Không tìm thấy nguồn.')
    const jobId = uid('job')
    stored.source.status = 'deleting'
    stored.source.deleted_at = nowIso()
    this.jobs.set(jobId, { id: jobId, kind: 'delete', target_id: sourceId, state: 'running', retry_count: 0 })
    setTimeout(() => {
      stored.purged = true
      stored.content = null
      Object.assign(this.jobs.get(jobId)!, { state: 'succeeded' })
    }, 2500)
    return { job_id: jobId }
  }

  async getSourceContent(sourceId: string, parseRevisionId: string) {
    await wait(150)
    const stored = this.sources.get(sourceId)
    if (!stored) throw err(404, 'source_not_found', 'Không tìm thấy nguồn.')
    if (stored.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    if (!stored.content || stored.content.parse_revision_id !== parseRevisionId) {
      throw err(404, 'revision_not_found', 'Không tìm thấy phiên bản parse được yêu cầu.')
    }
    return clone(stored.content)
  }

  async getJob(jobId: string) {
    await wait(80)
    const job = this.jobs.get(jobId)
    if (!job) throw err(404, 'job_not_found', 'Không tìm thấy job.')
    return clone(job)
  }

  // ---------------------------------------------------------------- bài giảng

  async setAsrGlossary(workspaceId: string, terms: string[]) {
    await wait(120)
    const workspace = this.requireWorkspace(workspaceId)
    // Cùng giới hạn với media/asr.clean_glossary.
    const seen = new Set<string>()
    const clean: string[] = []
    for (const raw of terms) {
      const term = raw.normalize('NFC').replace(/\p{C}/gu, '').replace(/\s+/g, ' ').trim()
      if (!term || seen.has(term.toLocaleLowerCase())) continue
      if ([...term].length > 60) throw err(422, 'invalid_glossary', `Thuật ngữ dài quá 60 ký tự: ${term.slice(0, 30)}…`)
      seen.add(term.toLocaleLowerCase())
      clean.push(term)
    }
    if (clean.length > 40) throw err(422, 'invalid_glossary', 'Tối đa 40 thuật ngữ.')
    if (clean.join(', ').length > 400) throw err(422, 'invalid_glossary', 'Glossary dài quá 400 ký tự; giữ các thuật ngữ hay bị chép sai nhất.')
    workspace.asr_glossary = clean
    return { terms: [...clean], applies_to: 'new_uploads' as const }
  }

  private sessionView(stored: StoredSession): MediaUploadSession {
    const { session } = stored
    return clone({
      ...session,
      parts: session.parts.map((part) => {
        const uploaded = session.status === 'completed' || stored.uploaded.has(part.index)
        return session.status === 'open' && !uploaded
          ? { index: part.index, size: part.size, uploaded, upload_url: `mock-upload://${session.id}/${part.index}`, method: 'PUT' as const }
          : { index: part.index, size: part.size, uploaded }
      }),
    })
  }

  private requireSession(sessionId: string) {
    const stored = this.sessions.get(sessionId)
    if (!stored) throw err(404, 'upload_session_not_found', 'Không tìm thấy phiên tải lên.')
    return stored
  }

  createMediaSession(workspaceId: string, request: MediaSessionRequest, idempotencyKey: string) {
    return this.once(`media:${idempotencyKey}`, async () => {
      await wait(150)
      this.requireWorkspace(workspaceId)
      if (!/\.(mp4|m4v|webm)$/i.test(request.filename) || !request.mime.startsWith('video/')) {
        throw err(415, 'unsupported_media_type', 'MVP nhận video MP4/WebM.')
      }
      if (request.size_bytes > MAX_MEDIA_BYTES) throw err(413, 'file_too_large', `Video vượt giới hạn ${MAX_MEDIA_BYTES / 1024 / 1024} MB.`)
      const id = uid('ups')
      const count = Math.max(1, Math.ceil(request.size_bytes / MEDIA_PART_BYTES))
      const session: MediaUploadSession = {
        id,
        workspace_id: workspaceId,
        status: 'open',
        filename: request.filename,
        mime: request.mime,
        expected_bytes: request.size_bytes,
        part_bytes: MEDIA_PART_BYTES,
        part_count: count,
        language: request.language,
        keep_original: request.keep_original,
        caption: null,
        parts: Array.from({ length: count }, (_, index) => ({
          index,
          size: index < count - 1 ? MEDIA_PART_BYTES : request.size_bytes - MEDIA_PART_BYTES * (count - 1),
          uploaded: false,
        })),
      }
      const stored: StoredSession = { session, uploaded: new Set() }
      this.sessions.set(id, stored)
      return this.sessionView(stored)
    })
  }

  async getMediaSession(sessionId: string) {
    await wait(80)
    return this.sessionView(this.requireSession(sessionId))
  }

  uploadPart(url: string, blob: Blob, onProgress: (loaded: number) => void, signal?: AbortSignal) {
    const match = url.match(/^mock-upload:\/\/([^/]+)\/(\d+)$/)
    return new Promise<void>((resolve, reject) => {
      if (!match) return reject(err(400, 'invalid_upload_url', 'URL tải lên không hợp lệ.'))
      const stored = this.sessions.get(match[1])
      const index = Number(match[2])
      if (!stored || stored.session.status !== 'open') return reject(err(409, 'session_closed', 'Phiên upload đã đóng.'))
      if (blob.size !== stored.session.parts[index]?.size) return reject(err(400, 'part_size_mismatch', 'Kích thước phần không khớp.'))
      // Giả lập tiến độ gửi: ~10 bước, tối đa ~1,2 s mỗi phần.
      const steps = 10
      let step = 0
      const timer = setInterval(() => {
        step++
        onProgress(Math.round((blob.size * step) / steps))
        if (step >= steps) {
          clearInterval(timer)
          stored.uploaded.add(index)
          resolve()
        }
      }, 120)
      signal?.addEventListener(
        'abort',
        () => {
          clearInterval(timer)
          reject(new DOMException('Đã dừng tải lên.', 'AbortError'))
        },
        { once: true },
      )
    })
  }

  async attachCaption(sessionId: string, file: File) {
    await wait(120)
    const stored = this.requireSession(sessionId)
    if (stored.session.status !== 'open') throw err(409, 'session_closed', 'Phiên upload đã đóng.')
    if (!/\.(srt|vtt)$/i.test(file.name)) throw err(415, 'unsupported_media_type', 'Phụ đề phải là SRT hoặc WebVTT.')
    const text = await file.text()
    if (!parseCaptions(text).length) throw err(422, 'caption_invalid', 'File phụ đề không có cue hợp lệ.')
    stored.captionText = text
    stored.session.caption = { filename: file.name }
    return this.sessionView(stored)
  }

  async completeMediaSession(sessionId: string) {
    await wait(200)
    const stored = this.requireSession(sessionId)
    if (stored.session.status === 'completed' && stored.sourceId) return clone(this.sources.get(stored.sourceId)!.source)
    if (stored.session.status !== 'open') throw err(409, 'session_closed', 'Phiên upload đã hết hạn hoặc bị hủy.')
    const missing = stored.session.parts.filter((p) => !stored.uploaded.has(p.index)).map((p) => p.index)
    if (missing.length) throw err(409, 'upload_incomplete', `Còn ${missing.length} phần chưa tải lên đủ: ${missing.slice(0, 10).join(', ')}.`)
    const cues = stored.captionText ? parseCaptions(stored.captionText) : undefined
    const source = this.createLecture(stored.session.workspace_id, {
      kind: 'video',
      title: stored.session.filename.replace(/\.[^.]+$/, ''),
      language: stored.session.language,
      cues,
    })
    stored.session.status = 'completed'
    stored.sourceId = source.id
    return source
  }

  async cancelMediaSession(sessionId: string) {
    await wait(100)
    const stored = this.requireSession(sessionId)
    if (stored.session.status !== 'open') throw err(409, 'session_closed', 'Phiên upload đã đóng.')
    stored.session.status = 'cancelled'
    stored.uploaded.clear()
  }

  async addYoutube(workspaceId: string, input: { url: string; language: Language; title?: string; caption: File }) {
    await wait(250)
    this.requireWorkspace(workspaceId)
    const id = input.url.trim().match(/(?:v=|youtu\.be\/|shorts\/|embed\/|live\/)([A-Za-z0-9_-]{11})/)?.[1]
      ?? (/^[A-Za-z0-9_-]{11}$/.test(input.url.trim()) ? input.url.trim() : null)
    if (!id) throw err(422, 'invalid_youtube_url', 'Link YouTube không hợp lệ.')
    if (!/\.(srt|vtt)$/i.test(input.caption.name)) throw err(415, 'unsupported_media_type', 'Phụ đề phải là SRT hoặc WebVTT.')
    const cues = parseCaptions(await input.caption.text())
    if (!cues.length) throw err(422, 'caption_invalid', 'File phụ đề không có cue hợp lệ.')
    return this.createLecture(workspaceId, {
      kind: 'youtube',
      title: input.title?.trim() || `YouTube ${id}`,
      language: input.language,
      cues,
      url: `https://www.youtube.com/watch?v=${id}`,
    })
  }

  /** Tạo nguồn bài giảng và mô phỏng pipeline (probe → chép lời → đọc bảng → index). */
  private createLecture(
    workspaceId: string,
    options: { kind: SourceKind; title: string; language: Language; cues?: { start_ms: number; end_ms: number; text: string }[]; url?: string },
    immediate = false,
  ): Source {
    const id = uid('src')
    const jobId = uid('job')
    const youtube = options.kind === 'youtube'
    const captions = !!options.cues
    const base = sampleLecture(id, options.cues)
    const data: LectureData = youtube ? { segments: base.segments, regions: [], assets: [] } : base
    const duration = Math.max(LECTURE_DURATION_MS, ...data.segments.map((s) => s.end_ms))
    const capabilities: MediaCapabilities = {
      audio_available: !youtube,
      visual_available: !youtube,
      captions_available: captions,
      original_playback_available: youtube ? 'external' : 'audio_and_frames',
    }
    const coverage = lectureCoverage(data, captions, duration)
    const finalStatus: SourceStatus =
      (coverage.flagged_ratio ?? 0) > REVIEW_RATIO ? 'review_required' : youtube ? 'ready_limited' : 'ready'
    const stored: StoredSource = {
      content: null,
      purged: false,
      lecture: { revisions: new Map(), captions },
      source: {
        id,
        workspace_id: workspaceId,
        alias: this.nextAlias(workspaceId),
        kind: options.kind,
        title: options.title,
        url: options.url,
        authors: [],
        source_type: youtube ? 'web' : 'user_note',
        origin_group_id: youtube ? `yt-${id}` : `g-${id}`,
        independence_status: 'unknown',
        grouping_version: 1,
        status: 'uploaded',
        progress: 0.05,
        job_id: jobId,
        created_at: nowIso(),
        language: options.language,
        capabilities: { ...capabilities, audio_available: null, visual_available: null },
      },
    }
    this.sources.set(id, stored)
    this.jobs.set(jobId, { id: jobId, kind: 'ingest', target_id: id, state: 'queued', retry_count: 0 })
    const publish = () => {
      const parseRevisionId = uid('pr')
      stored.lecture!.revisions.set(parseRevisionId, buildTimeline(id, parseRevisionId, data, capabilities, coverage, null))
      stored.content = lectureContent(id, parseRevisionId, data)
      Object.assign(stored.source, {
        status: finalStatus,
        progress: 1,
        duration_ms: duration,
        capabilities,
        coverage,
        revision: {
          document_revision_id: uid('dr'),
          revision_no: 1,
          parse_revision_id: parseRevisionId,
          page_count: null,
          leaf_count: data.segments.length + new Set(data.regions.map((r) => r.text)).size,
        },
      })
      Object.assign(this.jobs.get(jobId)!, { state: 'succeeded', stage: 'publish' })
    }
    if (immediate) {
      stored.source.created_at = '2026-10-05T09:00:00.000Z'
      publish()
      return clone(stored.source)
    }
    const steps: [number, SourceStatus, number, string][] = [
      [500, 'parsing', 0.15, 'extract_audio'],
      [1400, 'parsing', 0.3, captions ? 'captions' : 'transcribe'],
      [2400, 'parsing', 0.55, 'visual_read'],
      [3200, 'chunking', 0.7, 'align'],
      [3900, 'embedding', 0.85, 'embedding'],
    ]
    for (const [at, status, progress, stage] of steps) {
      setTimeout(() => {
        if (stored.source.deleted_at) return
        Object.assign(stored.source, { status, progress })
        Object.assign(this.jobs.get(jobId)!, { state: 'running', stage })
      }, at)
    }
    setTimeout(() => !stored.source.deleted_at && publish(), 4600)
    return clone(stored.source)
  }

  async getTimeline(sourceId: string, parseRevisionId: string) {
    await wait(150)
    const stored = this.sources.get(sourceId)
    if (!stored || stored.purged) throw err(404, 'source_not_found', 'Không tìm thấy nguồn.')
    if (stored.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    const timeline = stored.lecture?.revisions.get(parseRevisionId)
    if (!timeline) throw err(404, 'not_found', 'Không tìm thấy timeline của phiên bản.')
    return clone(timeline)
  }

  async getMediaUrl(sourceId: string, assetId: string): Promise<MediaUrl> {
    await wait(60)
    const stored = this.sources.get(sourceId)
    if (!stored || stored.purged) throw err(404, 'source_not_found', 'Không tìm thấy nguồn.')
    if (stored.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    for (const timeline of stored.lecture?.revisions.values() ?? []) {
      const asset = timeline.assets.find((a) => a.id === assetId)
      if (asset) {
        const data: LectureData = { segments: timeline.segments, regions: timeline.regions, assets: timeline.assets }
        return { asset_id: asset.id, kind: asset.kind, mime: asset.mime, url: frameDataUrl(data, asset.id), expires_in: 300 }
      }
    }
    throw err(404, 'not_found', 'Không tìm thấy media.')
  }

  async createTranscriptRevision(sourceId: string, edit: TranscriptEdit) {
    await wait(200)
    const stored = this.sources.get(sourceId)
    if (!stored || stored.purged || !stored.lecture) throw err(404, 'source_not_found', 'Không tìm thấy nguồn.')
    if (stored.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    if (!edit.segments.length && !edit.regions.length) throw err(422, 'empty_edit', 'Không có thay đổi nào.')
    const base = stored.lecture.revisions.get(edit.base_revision_id)
    if (!base) throw err(404, 'not_found', 'Không tìm thấy phiên bản trích xuất.')
    if ([...this.jobs.values()].some((j) => j.target_id === sourceId && (j.state === 'queued' || j.state === 'running'))) {
      throw err(409, 'source_busy', 'Nguồn đang được xử lý; thử lại sau khi hoàn tất.')
    }
    const segmentEdits = new Map(edit.segments.map((e) => [e.id, e.text]))
    const regionEdits = new Map(edit.regions.map((e) => [e.id, e.text]))
    const unknown = [...segmentEdits.keys()].filter((id) => !base.segments.some((s) => s.id === id)).length
      + [...regionEdits.keys()].filter((id) => !base.regions.some((r) => r.id === id)).length
    if (unknown) throw err(422, 'unknown_items', `${unknown} mục không thuộc phiên bản này.`)
    const jobId = uid('job')
    const job: Job = { id: jobId, kind: 'reindex', target_id: sourceId, state: 'queued', retry_count: 0 }
    this.jobs.set(jobId, job)
    setTimeout(() => {
      if (stored.source.deleted_at) return
      // Đoạn đã sửa = đã được người đối chiếu: bỏ cờ cảnh báo, origin manual_edit (pipeline.apply_transcript_edit).
      const data: LectureData = {
        segments: base.segments.map((s) =>
          segmentEdits.has(s.id) ? { ...s, text: segmentEdits.get(s.id)!, origin: 'manual_edit', flags: [], reviewed: true } : s,
        ),
        regions: base.regions.map((r) => (regionEdits.has(r.id) ? { ...r, text: regionEdits.get(r.id)!, review_state: 'reviewed', flags: [] } : r)),
        assets: base.assets,
      }
      const parseRevisionId = uid('pr')
      const coverage = { ...lectureCoverage(data, stored.lecture!.captions, base.duration_ms ?? LECTURE_DURATION_MS), ...{ visual_read: base.coverage?.visual_read } }
      stored.lecture!.revisions.set(parseRevisionId, buildTimeline(sourceId, parseRevisionId, data, base.capabilities!, coverage, base.extraction_revision_id))
      stored.content = lectureContent(sourceId, parseRevisionId, data)
      const status: SourceStatus = (coverage.flagged_ratio ?? 0) > REVIEW_RATIO ? 'review_required' : stored.source.kind === 'youtube' ? 'ready_limited' : 'ready'
      Object.assign(stored.source, { status, coverage, revision: { ...stored.source.revision!, parse_revision_id: parseRevisionId } })
      Object.assign(job, { state: 'succeeded', stage: 'publish' })
    }, 1500)
    return clone(job)
  }

  // ------------------------------------------------------------ dịch tài liệu

  async listTranslationLanguages(): Promise<TranslationLanguage[]> {
    await wait(60)
    return [
      { code: 'vi', name: 'Vietnamese' },
      { code: 'en', name: 'English (US)' },
      { code: 'zh-Hans', name: 'Simplified Chinese' },
      { code: 'ja', name: 'Japanese' },
      { code: 'ko', name: 'Korean' },
      { code: 'fr', name: 'French' },
      { code: 'de', name: 'German' },
    ]
  }

  /** Mock không gọi LLM: "bản dịch" là đoạn gốc gắn nhãn ngôn ngữ, đủ để thử giao diện song song và tiến độ. */
  async requestTranslation(sourceId: string, targetLang: string): Promise<SourceTranslation> {
    await wait(150)
    const stored = this.sources.get(sourceId)
    if (!stored || stored.purged) throw err(404, 'not_found', 'Nguồn không tồn tại hoặc bạn không có quyền truy cập.')
    if (stored.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    if (!QUERYABLE.includes(stored.source.status) || !stored.content) throw err(409, 'source_not_ready', 'Nguồn chưa xử lý xong nên chưa dịch được.')
    const languages = await this.listTranslationLanguages()
    if (!languages.some((l) => l.code === targetLang)) throw err(422, 'unsupported_language', 'Ngôn ngữ đích chưa được hỗ trợ.')
    const revision = stored.content.parse_revision_id
    const key = `${revision}:${targetLang}`
    const existing = this.translations.get(key)
    if (existing && existing.status !== 'failed') return clone({ ...existing, segments: undefined } as unknown as SourceTranslation)
    const text = stored.content.text
    const paragraphs: { start: number; end: number; text: string }[] = []
    let at = 0
    for (const part of text.split('\n\n')) {
      const start = text.indexOf(part, at)
      at = start + part.length
      if (part.trim()) paragraphs.push({ start: utf16ToCp(text, start), end: utf16ToCp(text, at), text: part })
    }
    const detail: TranslationDetail = {
      id: uid('tr'),
      source_id: sourceId,
      parse_revision_id: revision,
      target_lang: targetLang,
      status: 'queued',
      progress: 0,
      segment_count: paragraphs.length,
      char_count: text.length,
      flagged_count: 0,
      model: 'mock',
      error: null,
      created_at: nowIso(),
      finished_at: null,
      segments: [],
    }
    this.translations.set(key, detail)
    const lecture = stored.lecture?.revisions.get(revision)
    paragraphs.forEach((paragraph, index) => {
      setTimeout(() => {
        const segment = lecture?.segments.find((s) => s.text === paragraph.text)
        detail.status = index === paragraphs.length - 1 ? 'succeeded' : 'running'
        detail.progress = (index + 1) / paragraphs.length
        detail.segments.push({
          node_id: `leaf_${sourceId}_${paragraph.start}`,
          ordinal: index,
          original: paragraph.text,
          text: `〔${targetLang}〕 ${paragraph.text}`,
          flags: [],
          char_start: paragraph.start,
          char_end: paragraph.end,
          page_start: null,
          page_end: null,
          modality: lecture ? (segment ? 'speech' : 'board') : 'text',
          start_ms: segment?.start_ms ?? null,
          end_ms: segment?.end_ms ?? null,
        })
        if (detail.status === 'succeeded') detail.finished_at = nowIso()
      }, 300 + index * 120)
    })
    return clone({ ...detail, segments: undefined } as unknown as SourceTranslation)
  }

  async getTranslation(sourceId: string, parseRevisionId: string, targetLang: string): Promise<TranslationDetail> {
    await wait(80)
    const stored = this.sources.get(sourceId)
    if (stored?.source.deleted_at) throw err(410, 'source_deleted', 'Nguồn đã bị xóa; nội dung không còn được hiển thị.')
    const detail = this.translations.get(`${parseRevisionId}:${targetLang}`)
    if (!detail || detail.source_id !== sourceId) throw err(404, 'not_found', 'Bản dịch không tồn tại hoặc bạn không có quyền truy cập.')
    return clone(detail)
  }

  // ------------------------------------------------------------- run helpers

  private scope(workspaceId: string, sourceIds: string[]) {
    const scope = this.visibleSources(workspaceId).filter(
      (s) => QUERYABLE.includes(s.source.status) && !s.source.deleted_at && (sourceIds.length === 0 || sourceIds.includes(s.source.id)),
    )
    if (!scope.length) throw err(422, 'empty_scope', 'Phạm vi không có nguồn nào sẵn sàng.')
    const lectures = scope.filter((s) => isLectureKind(s.source.kind))
    if (lectures.length && lectures.length < scope.length) {
      // Như backend: một run chỉ dùng một model profile; bài giảng (lecture_vi_v1) và tài liệu (product) khác profile.
      throw err(
        422,
        'profile_index_missing',
        `Phạm vi trộn bài giảng (${lectures.map((s) => s.source.alias).join(', ')}) và tài liệu: chưa có chỉ mục chung cùng profile. Hãy hỏi riêng từng loại.`,
      )
    }
    return scope
  }

  private evidence(
    scope: StoredSource[],
    seed: { alias: string; quote: string; support: number; contradiction: number },
    role: EvidenceRef['role'],
  ): EvidenceRef | null {
    const stored = scope.find((s) => s.source.alias === seed.alias)
    if (!stored?.content) return null
    const text = stored.content.text
    const at = text.indexOf(seed.quote)
    if (at < 0) throw new Error(`Mock: không tìm thấy trích dẫn trong ${seed.alias}`)
    // Leaf = đoạn văn chứa trích dẫn.
    const previousBreak = text.lastIndexOf('\n\n', at)
    const leafStart = previousBreak < 0 ? 0 : previousBreak + 2
    const nextBreak = text.indexOf('\n\n', at + seed.quote.length)
    const leafEnd = nextBreak < 0 ? text.length : nextBreak
    const cpAt = utf16ToCp(text, at)
    const page = stored.content.pages?.find((p) => cpAt >= p.char_start && cpAt < p.char_end)?.page ?? null
    return {
      id: uid('ev'),
      role,
      source_id: stored.source.id,
      source_alias: seed.alias,
      node_id: `leaf_${stored.source.id}_${leafStart}`,
      parse_revision_id: stored.content.parse_revision_id,
      page_start: page,
      page_end: page,
      char_start: utf16ToCp(text, leafStart),
      char_end: utf16ToCp(text, leafEnd),
      quote_start: cpAt,
      quote_end: utf16ToCp(text, at + seed.quote.length),
      evidence_snapshot: seed.quote,
      support: seed.support,
      contradiction: seed.contradiction,
      verifier_revision: 'deberta-v3-base-mnli-fever-anli@mock',
      position: 'exact',
    }
  }

  private buildClaim(scope: StoredSource[], seed: SeedClaim): ClaimResult | null {
    const primary = this.evidence(scope, seed.primary, 'primary')
    if (!primary) return null
    const groupOf = (alias: string) => scope.find((s) => s.source.alias === alias)?.source.origin_group_id
    const primaryGroup = groupOf(seed.primary.alias)!
    const others = (seed.others ?? [])
      .filter((o) => groupOf(o.alias) !== undefined && groupOf(o.alias) !== primaryGroup)
      .map((o) => ({ group: groupOf(o.alias)!, evidence: this.evidence(scope, o, o.role) }))
      .filter((o): o is { group: string; evidence: EvidenceRef } => o.evidence !== null)
    const reviewed = (group: string) =>
      REVIEWED_INDEPENDENT.some(([a, b]) => (a === primaryGroup && b === group) || (b === primaryGroup && a === group))
    const contradicting = others.filter((o) => o.evidence.role === 'contradicting')
    const corroborating = others.filter((o) => o.evidence.role === 'corroborating')
    // CORROBORATED chỉ khi quan hệ độc lập đã được đánh giá (mục 6.2, 8.3).
    const status = contradicting.length ? 'contested' : corroborating.some((o) => reviewed(o.group)) ? 'corroborated' : 'supported'
    return {
      id: seed.id,
      text: seed.text,
      section_key: seed.section_key,
      status,
      cross_check_status: seed.crossCheckIncomplete ? 'incomplete' : 'completed',
      verification_method: 'nli_visible_evidence',
      generator_confidence: seed.confidence,
      evidence: [primary, ...others.map((o) => o.evidence)],
    }
  }

  private plan(runId: string, scope: StoredSource[], budget: Budget, sectionKeys?: string[]) {
    const seeds = SEED_CLAIMS.filter((c) => !sectionKeys || sectionKeys.includes(c.section_key))
    const claims = seeds
      .map((seed) => this.buildClaim(scope, seed))
      .filter((c): c is ClaimResult => c !== null)
      .slice(0, BLOCKS_PER_BUDGET[budget])
    const context = this.contextFor(runId, claims)
    const rejected: RejectedWithSupport[] = claims.length
      ? SEED_REJECTED.filter((r) => !sectionKeys || sectionKeys.includes(r.section_key)).map((r) => ({ ...r, id: `${r.id}_${runId}` }))
      : []
    return { claims, context, rejected }
  }

  private contextFor(runId: string, claims: ClaimResult[]): ContextBlock[] {
    return claims.map((claim, index) => {
      const primary = claim.evidence[0]
      const block: ContextBlock = {
        id: `cb_${runId}_${claim.id}`,
        context_id: `C${index + 1}`,
        node_id: primary.node_id,
        source_alias: primary.source_alias,
        parse_revision_id: primary.parse_revision_id,
        page_start: primary.page_start,
        page_end: primary.page_end,
        rank: index + 1,
        visible_text: primary.evidence_snapshot,
        token_count: Math.ceil(primary.evidence_snapshot.length / 4) + 24,
        truncated: false,
        trace: {
          raw_ce_score: Number((4.1 - index * 0.63).toFixed(3)),
          sbert_similarity: Number((0.71 - index * 0.05).toFixed(3)),
          rrf_score: Number((2 / (60 + index + 1)).toFixed(5)),
          packing_utility: Number((4.1 - index * 0.63).toFixed(3)),
        },
      }
      if (primary.locator) {
        block.modality = primary.modality
        block.start_ms = primary.locator.start_ms ?? null
        block.end_ms = primary.locator.end_ms ?? null
      }
      primary.context_block_id = block.id
      return block
    })
  }

  /** QA trên bài giảng: citation có locator thời gian/vùng bảng; claim "tốt hơn" bị chốt như vi_text.claim_guard. */
  private lecturePlan(runId: string, scope: StoredSource[]) {
    const claims: ClaimResult[] = []
    for (const stored of scope) {
      const timeline = stored.lecture?.revisions.get(stored.source.revision!.parse_revision_id)
      if (!timeline) continue
      const board = timeline.regions.find((r) => r.included && /k\s*=\s*60/.test(r.text))
      const speech = timeline.segments.find((s) => s.text.includes('32 giây'))
      const items: { id: string; text: string; quote: string; modality: Modality; item: LocatorItem }[] = []
      if (board) {
        items.push({
          id: `cl_k_${stored.source.id}`,
          text: 'Hằng số k trong Agreement Ranking bằng 60.',
          quote: board.text,
          modality: 'board',
          item: {
            type: 'region', id: board.id, start_ms: board.visible_from_ms, end_ms: board.visible_to_ms,
            asset_id: board.frame_asset_id, bbox: board.bbox, flags: board.flags, reviewed: board.review_state === 'reviewed',
          },
        })
      }
      if (speech) {
        items.push({
          id: `cl_index_${stored.source.id}`,
          text: 'Lập chỉ mục 70 bài báo với Agreement Ranking mất 32 giây.',
          quote: speech.text,
          modality: stored.lecture!.captions ? 'caption' : 'speech',
          item: {
            type: 'segment', id: speech.id, start_ms: speech.start_ms, end_ms: speech.end_ms,
            asset_id: speech.audio_asset_id, bbox: null, flags: speech.flags, reviewed: speech.reviewed,
          },
        })
      }
      for (const { id, text, quote, modality, item } of items) {
        const evidence = this.evidence(scope, { alias: stored.source.alias, quote, support: 0.97, contradiction: 0.01 }, 'primary')
        if (!evidence) continue
        evidence.verifier_revision = 'mdeberta-v3-base-mnli-xnli@mock'
        evidence.modality = modality
        evidence.transcription_state = item.reviewed ? 'reviewed' : 'automatic'
        evidence.locator = {
          modality, start_ms: item.start_ms, end_ms: item.end_ms, precision: item.type, items: [item],
          extraction_revision_id: timeline.extraction_revision_id,
        }
        claims.push({
          id: `${id}_${runId}`,
          text,
          status: 'supported',
          cross_check_status: 'completed',
          verification_method: 'nli_visible_evidence',
          generator_confidence: 0.9,
          support_basis: item.reviewed ? 'reviewed_transcript' : 'automatic_transcript',
          evidence: [evidence],
        })
      }
    }
    const rejected: RejectedWithSupport[] = claims.length
      ? [{ id: `rj_superior_${runId}`, text: 'Agreement Ranking tốt hơn RAPTOR trên PeerQA.', reason: 'non_inferiority_overclaimed', support: 0.97 }]
      : []
    return { claims, context: this.contextFor(runId, claims), rejected }
  }

  private metrics(claims: ClaimResult[], rejected: { support?: number | null }[], context: ContextBlock[], jsonErrors: number, latency: number): RunMetrics {
    const generated = claims.length + rejected.length
    const accepted = claims.length
    const supportsAccepted = claims.map((c) => c.evidence[0].support)
    const supportsGenerated = [...supportsAccepted, ...rejected.map((r) => r.support ?? 0)]
    const risk = (values: number[]) => (values.length ? 1 - values.reduce((a, b) => a + b, 0) / values.length : null)
    const byGroup = new Map<string, number>()
    for (const claim of claims) {
      const group = this.sources.get(claim.evidence[0].source_id)?.source.origin_group_id ?? 'unknown'
      byGroup.set(group, (byGroup.get(group) ?? 0) + 1)
    }
    return {
      generated_count: generated,
      accepted_count: accepted,
      json_error_count: jsonErrors,
      primary_attribution_risk_generated: risk(supportsGenerated),
      primary_attribution_risk_accepted: risk(supportsAccepted),
      unsupported_generated_rate: rate(rejected.length, generated),
      citation_survival_rate: rate(accepted, generated),
      corroboration_rate: rate(claims.filter((c) => c.status === 'corroborated').length, accepted),
      contest_rate: rate(claims.filter((c) => c.status === 'contested').length, accepted),
      source_concentration: accepted ? Math.max(...byGroup.values()) / accepted : null,
      origin_groups_cited: byGroup.size,
      cross_check_completion_rate: rate(claims.filter((c) => c.cross_check_status === 'completed').length, accepted),
      context_tokens: context.reduce((sum, b) => sum + b.token_count, 0),
      latency_ms: { queue: 120, retrieval: 210, rerank: 640, generation: Math.round(latency * 0.55), verification: 380, cross_check: 290, total: latency },
    }
  }

  private provenance(scope: StoredSource[]): Provenance {
    if (scope.length && scope.every((s) => isLectureKind(s.source.kind))) {
      return {
        ...this.provenance([]),
        profile: 'lecture_vi_v1',
        verifier_revision: 'mdeberta-v3-base-mnli-xnli@mock',
        models: {
          embedding: 'BAAI/bge-m3',
          reranker: 'BAAI/bge-reranker-v2-m3',
          sbert: 'sentence-transformers/paraphrase-multilingual-mpnet-base-v2',
          nli: 'MoritzLaurer/mDeBERTa-v3-base-mnli-xnli',
          llm: 'mock',
        },
        source_snapshot: scope.map((s) => ({
          source_id: s.source.id,
          alias: s.source.alias,
          document_revision_id: s.source.revision!.document_revision_id,
          parse_revision_id: s.source.revision!.parse_revision_id,
        })),
      }
    }
    return {
      profile: 'product',
      config_hash: 'cfg_mock_0001',
      code_hash: 'code_mock_0001',
      edahr_commit: null,
      verifier_revision: 'deberta-v3-base-mnli-fever-anli@mock',
      calibration_artifact: null,
      grouping_version: 1,
      models: {
        embedding: 'BAAI/bge-m3',
        reranker: 'BAAI/bge-reranker-v2-m3',
        sbert: 'sentence-transformers/multi-qa-mpnet-base-cos-v1',
        nli: 'MoritzLaurer/DeBERTa-v3-base-mnli-fever-anli',
        llm: 'mock',
      },
      source_snapshot: scope.map((s) => ({
        source_id: s.source.id,
        alias: s.source.alias,
        document_revision_id: s.source.revision!.document_revision_id,
        parse_revision_id: s.source.revision!.parse_revision_id,
      })),
    }
  }

  private emitClaims(timeline: Timeline, claims: ClaimResult[]) {
    timeline.stage('generate', 600)
    timeline.stage('verify', 700)
    // Chỉ claim đã kiểm chứng mới được phát; đối chứng chéo cập nhật lại cùng claim.
    claims.forEach((c) =>
      timeline.push(
        { type: 'claim_verified', claim: { ...c, status: 'supported', cross_check_status: 'not_run', evidence: c.evidence.slice(0, 1) } },
        350,
      ),
    )
    if (claims.length) {
      timeline.stage('cross_check', 400)
      claims.forEach((c) => timeline.push({ type: 'claim_verified', claim: c }, 300))
    }
  }

  private store(run: Run, timeline: Timeline): RunAccepted {
    timeline.push({ type: 'done', status: run.status }, 300)
    this.runs.set(run.run_id, { final: run, planned: timeline.events, startedAt: Date.now(), subscribers: new Set() })
    return { run_id: run.run_id, status_url: `/api/runs/${run.run_id}`, stream_url: `/api/runs/${run.run_id}/stream` }
  }

  private baseRun(runId: string, workspaceId: string, mode: Run['mode'], query: string, budget: Budget, sourceIds: string[], scope: StoredSource[]): Run {
    return {
      run_id: runId,
      workspace_id: workspaceId,
      mode,
      profile: scope.length && scope.every((s) => isLectureKind(s.source.kind)) ? 'lecture_vi_v1' : 'product',
      query,
      budget: { context_tokens: budget },
      source_ids: sourceIds,
      status: 'succeeded',
      cancel_requested: false,
      claims: [],
      rejected_claims: [],
      context: [],
      provenance: this.provenance(scope),
      created_at: nowIso(),
    }
  }

  private static publicRejected(rejected: RejectedWithSupport[]): RejectedClaim[] {
    return rejected.map(({ id, text, reason, section_key, support }) => ({ id, text, reason, section_key, support }))
  }

  // ---------------------------------------------------------------------- QA

  askQuestion(workspaceId: string, request: QaRequest, idempotencyKey: string) {
    return this.once(`qa:${idempotencyKey}`, async () => {
      await wait(200)
      this.requireWorkspace(workspaceId)
      if (request.question.trim().length < 3) throw err(422, 'invalid_question', 'Câu hỏi quá ngắn.')
      const scope = this.scope(workspaceId, request.source_ids)
      const runId = uid('run')
      const q = request.question.toLowerCase()
      const queryType = classifyQuery(request.question)
      const run = this.baseRun(runId, workspaceId, 'qa', request.question.trim(), request.budget, request.source_ids, scope)
      run.query_type = queryType
      run.rerun_of = request.rerun_of
      const timeline = new Timeline()
      timeline.stage('queued', 100)
      timeline.stage('classify', 400)
      timeline.stage('retrieve')

      if (q.includes('[fail]')) {
        run.status = 'failed'
        run.error = {
          code: 'llm_timeout',
          message: 'Nhà cung cấp LLM không phản hồi trong thời hạn; đã hết số lần thử lại.',
          retryable: true,
          trace_id: uid('trace'),
        }
        run.metrics = this.metrics([], [], [], 0, 4200)
        return this.store(run, timeline)
      }

      timeline.stage('rank', 600)
      timeline.stage('pack', 500)
      const groups = new Set(scope.map((s) => s.source.origin_group_id))
      let answer: AnswerStatus = 'answered'
      const lectureScope = scope.every((s) => isLectureKind(s.source.kind))
      let plan = lectureScope ? this.lecturePlan(runId, scope) : this.plan(runId, scope, request.budget)
      let jsonErrors = 0

      if (queryType === 'comparative_multi_hop' && groups.size < 2) {
        // COMPARATIVE thiếu một phía → không nhét nguồn không liên quan (mục 7.2 bước 6).
        answer = 'insufficient_evidence'
        run.error = {
          code: 'comparative_missing_side',
          message: 'Câu hỏi so sánh cần bằng chứng từ ít nhất hai nhóm nguồn; phạm vi hiện chỉ có một nhóm.',
          retryable: false,
        }
        plan = { claims: [], context: plan.context, rejected: [] }
      } else if (q.includes('[invalid]')) {
        timeline.stage('generate', 600)
        answer = 'model_output_invalid'
        jsonErrors = 1
        run.error = { code: 'local_invalid_json', message: 'Mô hình trả về JSON sai hợp đồng citation; lượt này không có claim.', retryable: true }
        plan = { claims: [], context: plan.context, rejected: [] }
      } else if (q.includes('[verifier-down]')) {
        timeline.stage('generate', 600)
        timeline.stage('verify', 700)
        answer = 'verification_unavailable'
        run.error = { code: 'verifier_timeout', message: 'Dịch vụ NLI không phản hồi; claim không được mặc định là đạt.', retryable: true }
        plan = {
          claims: [],
          context: plan.context,
          rejected: plan.claims.map((c) => ({ id: `rj_${c.id}_${runId}`, text: c.text, reason: 'verification_unavailable', support: null })),
        }
      } else {
        this.emitClaims(timeline, plan.claims)
        if (!plan.claims.length) answer = 'insufficient_evidence'
      }

      run.answer_status = answer
      run.claims = plan.claims
      run.rejected_claims = MockApi.publicRejected(plan.rejected)
      run.context = plan.context
      run.metrics = this.metrics(plan.claims, plan.rejected, plan.context, jsonErrors, 3480)
      return this.store(run, timeline)
    })
  }

  // --------------------------------------------------------------- synthesis

  async proposeOutline(workspaceId: string, request: OutlineRequest) {
    await wait(900)
    this.requireWorkspace(workspaceId)
    this.scope(workspaceId, request.source_ids)
    if (!request.topic.trim()) throw err(422, 'invalid_topic', 'Cần nhập chủ đề.')
    return clone(SEED_OUTLINE)
  }

  startSynthesis(workspaceId: string, request: SynthesisRequest, idempotencyKey: string) {
    return this.once(`syn:${idempotencyKey}`, async () => {
      await wait(200)
      this.requireWorkspace(workspaceId)
      if (!request.outline.length) throw err(422, 'empty_outline', 'Dàn ý cần ít nhất một mục.')
      const scope = this.scope(workspaceId, request.source_ids)
      const runId = uid('run')
      const run = this.baseRun(runId, workspaceId, 'synthesis', request.topic.trim(), request.budget, request.source_ids, scope)
      run.query_type = 'global_synthesis'
      run.outline = request.outline
      const timeline = new Timeline()
      timeline.stage('queued', 100)
      timeline.stage('outline', 300)
      const sections: ReportSection[] = []
      const rejected: RejectedWithSupport[] = []
      let failed = 0
      for (const outline of request.outline) {
        timeline.stage('subquestion', 500, `${STAGE_LABELS.subquestion}: ${outline.title}`)
        let section: ReportSection
        if (outline.title.includes('[fail]')) {
          failed++
          section = { key: outline.key, title: outline.title, status: 'failed', paragraphs: [{ template_text: TEMPLATE_FAILED, claim_ids: [] }] }
        } else {
          const plan = this.plan(runId, scope, request.budget, [outline.key])
          this.emitClaims(timeline, plan.claims)
          const offset = run.context.length
          run.context.push(...plan.context.map((b, i) => ({ ...b, context_id: `C${offset + i + 1}`, rank: offset + i + 1 })))
          run.claims.push(...plan.claims)
          rejected.push(...plan.rejected)
          section = plan.claims.length
            ? {
                key: outline.key,
                title: outline.title,
                status: 'complete',
                paragraphs: [{ template_text: TEMPLATE_LEAD, claim_ids: plan.claims.map((c) => c.id) }],
              }
            : { key: outline.key, title: outline.title, status: 'insufficient_evidence', paragraphs: [{ template_text: TEMPLATE_EMPTY, claim_ids: [] }] }
        }
        sections.push(section)
        timeline.stage('compose', 300)
        timeline.push({ type: 'section', section }, 300)
      }
      run.sections = sections
      run.rejected_claims = MockApi.publicRejected(rejected)
      run.answer_status = run.claims.length ? 'answered' : 'insufficient_evidence'
      if (failed) {
        run.status = 'partial'
        run.error = { code: 'subquestion_failed', message: `${failed} mục không hoàn tất; các mục còn lại vẫn được publish.`, retryable: true }
      }
      run.metrics = this.metrics(run.claims, rejected, run.context, 0, 9100)
      return this.store(run, timeline)
    })
  }

  // --------------------------------------------------------------------- runs

  private effectiveEvents(stored: StoredRun): PlannedEvent[] {
    if (stored.cancelledAt === undefined) return stored.planned
    const cancelledAt = stored.cancelledAt
    const kept = stored.planned.filter((e) => e.at <= cancelledAt && e.event.type !== 'done')
    const seq = (kept.at(-1)?.event.seq ?? 0) + 1
    return [...kept, { at: cancelledAt + 400, event: { seq, type: 'done', status: 'cancelled' } }]
  }

  /** Trạng thái run tại thời điểm hiện tại, dựng lại từ các event đã phát. */
  private snapshot(stored: StoredRun): Run {
    const events = this.effectiveEvents(stored)
    const elapsed = Date.now() - stored.startedAt
    const endAt = events.at(-1)?.at ?? 0
    const finished = elapsed >= endAt
    const finishedAt = new Date(stored.startedAt + endAt).toISOString()
    let run: Run
    if (finished && stored.cancelledAt === undefined) {
      run = clone(stored.final)
      run.finished_at = finishedAt
    } else {
      const seen = events.filter((e) => e.at <= elapsed)
      const claims = new Map<string, ClaimResult>()
      const sections: ReportSection[] = []
      for (const { event } of seen) {
        if (event.type === 'claim_verified') claims.set(event.claim.id, event.claim)
        if (event.type === 'section') sections.push(event.section)
      }
      const stages = seen.filter((e) => e.event.type === 'stage').length
      const status: RunStatus = finished ? 'cancelled' : stages <= 1 ? 'queued' : 'running'
      run = {
        ...clone(stored.final),
        status,
        answer_status: undefined,
        error: undefined,
        claims: clone([...claims.values()]),
        rejected_claims: [],
        sections: stored.final.mode === 'synthesis' ? clone(sections) : undefined,
        metrics: undefined,
        cancel_requested: stored.cancelledAt !== undefined,
        finished_at: finished ? finishedAt : undefined,
      }
    }
    return this.maskDeleted(run)
  }

  /** Nguồn đã tombstone: giữ tham chiếu nhưng ẩn nội dung (mục 6.5). */
  private maskDeleted(run: Run): Run {
    const deleted = (id: string) => !!this.sources.get(id)?.source.deleted_at
    const deletedAliases = new Set(
      [...this.sources.values()].filter((s) => s.source.deleted_at && s.source.workspace_id === run.workspace_id).map((s) => s.source.alias),
    )
    for (const claim of run.claims) {
      for (const e of claim.evidence) {
        if (deleted(e.source_id)) Object.assign(e, { source_deleted: true, evidence_snapshot: '' })
      }
    }
    for (const block of run.context) {
      if (deletedAliases.has(block.source_alias)) block.visible_text = ''
    }
    return run
  }

  private requireRun(runId: string) {
    const stored = this.runs.get(runId)
    if (!stored) throw err(404, 'run_not_found', 'Không tìm thấy lượt chạy.')
    return stored
  }

  async listRuns(workspaceId: string, cursor?: string): Promise<Page<RunSummary>> {
    await wait(120)
    this.requireWorkspace(workspaceId)
    const all = [...this.runs.values()]
      .filter((r) => r.final.workspace_id === workspaceId)
      .map((r) => this.snapshot(r))
      .sort((a, b) => b.created_at.localeCompare(a.created_at))
      .map((run) => ({
        run_id: run.run_id,
        mode: run.mode,
        query: run.query,
        status: run.status,
        answer_status: run.answer_status,
        created_at: run.created_at,
        budget: run.budget,
        rerun_of: run.rerun_of,
        accepted_count: run.claims.length,
      }))
    const start = cursor ? Number(cursor) : 0
    return { items: all.slice(start, start + PAGE_SIZE), next_cursor: start + PAGE_SIZE < all.length ? String(start + PAGE_SIZE) : null }
  }

  async getRun(runId: string) {
    await wait(120)
    return this.snapshot(this.requireRun(runId))
  }

  async cancelRun(runId: string) {
    await wait(150)
    const stored = this.requireRun(runId)
    if (stored.cancelledAt !== undefined) return
    const elapsed = Date.now() - stored.startedAt
    if (elapsed >= (stored.planned.at(-1)?.at ?? 0)) throw err(409, 'run_not_cancellable', 'Lượt chạy đã kết thúc.')
    stored.cancelledAt = elapsed
    for (const subscriber of stored.subscribers) this.schedule(stored, subscriber)
  }

  private schedule(stored: StoredRun, subscriber: Subscriber) {
    subscriber.timers.forEach(clearTimeout)
    const elapsed = Date.now() - stored.startedAt
    subscriber.timers = this.effectiveEvents(stored).map(({ at, event }) =>
      setTimeout(() => subscriber.onEvent(clone(event)), Math.max(0, at - elapsed)),
    )
  }

  streamRun(runId: string, onEvent: (event: RunEvent) => void, onConnection?: (state: 'open' | 'reconnecting') => void): StreamHandle {
    const stored = this.runs.get(runId)
    if (!stored) {
      const timer = setTimeout(
        () => onEvent({ seq: 1, type: 'error', error: { code: 'run_not_found', message: 'Không tìm thấy lượt chạy.', retryable: false } }),
        0,
      )
      return { close: () => clearTimeout(timer) }
    }
    const subscriber: Subscriber = { onEvent, timers: [] }
    stored.subscribers.add(subscriber)
    const open = setTimeout(() => onConnection?.('open'), 0)
    // Phát lại từ đầu như kết nối không có Last-Event-ID; client dedup theo seq.
    this.schedule(stored, subscriber)
    return {
      close: () => {
        clearTimeout(open)
        subscriber.timers.forEach(clearTimeout)
        stored.subscribers.delete(subscriber)
      },
    }
  }

  async exportRun(runId: string, format: ExportFormat) {
    const run = await this.getRun(runId)
    if (!['succeeded', 'partial'].includes(run.status)) throw err(409, 'run_not_finished', 'Chỉ xuất được lượt chạy đã hoàn tất.')
    if (format === 'json') return new Blob([JSON.stringify(run, null, 2)], { type: 'application/json' })
    if (format === 'md') return new Blob([runToMarkdown(run)], { type: 'text/markdown' })
    throw err(501, 'export_format_unavailable', `Xuất ${format.toUpperCase()} thuộc giai đoạn G4, chưa có trong MVP.`)
  }
}

function citation(claim: ClaimResult) {
  const e = claim.evidence[0]
  return `[${e.source_alias}${e.page_start ? `, tr. ${e.page_start}` : ''}]`
}

/** Xuất Markdown từ claim/evidence đã persist; không gọi LLM viết lại (mục 11). */
export function runToMarkdown(run: Run) {
  const lines = [`# ${run.query}`, '', `> Run ${run.run_id} · ${run.status} · ${run.answer_status ?? '—'} · profile ${run.profile}`, '']
  const byId = new Map(run.claims.map((c) => [c.id, c]))
  if (run.mode === 'synthesis' && run.sections) {
    for (const section of run.sections) {
      lines.push(`## ${section.title}`, '', section.paragraphs.map((p) => p.template_text).join(' '), '')
      for (const id of section.paragraphs.flatMap((p) => p.claim_ids)) {
        const claim = byId.get(id)
        if (claim) lines.push(`- ${claim.text} ${citation(claim)}`)
      }
      lines.push('')
    }
  } else {
    for (const claim of run.claims) lines.push(`- ${claim.text} ${citation(claim)} — ${claim.status}`)
    lines.push('')
  }
  lines.push('## Bằng chứng', '')
  for (const claim of run.claims) {
    for (const e of claim.evidence) {
      const body = e.source_deleted ? '_[nguồn đã xóa — nội dung đã ẩn]_' : `"${e.evidence_snapshot}"`
      lines.push(`- **${e.source_alias}**${e.page_start ? ` tr. ${e.page_start}` : ''} (${e.role}, revision ${e.parse_revision_id}): ${body}`)
    }
  }
  return lines.join('\n')
}
