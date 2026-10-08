import type {
  Accepted,
  ApiErrorBody,
  ExportFormat,
  Job,
  OutlineRequest,
  OutlineSection,
  Page,
  QaRequest,
  Readiness,
  Run,
  RunAccepted,
  RunEvent,
  RunSummary,
  Source,
  SourceContent,
  SynthesisRequest,
  Workspace,
} from './types'

export interface StreamHandle {
  close(): void
}

export interface Api {
  readonly mode: 'mock' | 'http'
  getReadiness(): Promise<Readiness>

  listWorkspaces(): Promise<Workspace[]>
  createWorkspace(name: string): Promise<Workspace>
  getWorkspace(id: string): Promise<Workspace>

  listSources(workspaceId: string, cursor?: string): Promise<Page<Source>>
  uploadSource(workspaceId: string, file: File, idempotencyKey: string): Promise<Source>
  addRemoteSource(workspaceId: string, input: { url: string } | { doi: string }, idempotencyKey: string): Promise<Source>
  deleteSource(sourceId: string): Promise<Accepted>
  getSourceContent(sourceId: string, parseRevisionId: string): Promise<SourceContent>
  getJob(jobId: string): Promise<Job>

  askQuestion(workspaceId: string, request: QaRequest, idempotencyKey: string): Promise<RunAccepted>
  proposeOutline(workspaceId: string, request: OutlineRequest): Promise<OutlineSection[]>
  startSynthesis(workspaceId: string, request: SynthesisRequest, idempotencyKey: string): Promise<RunAccepted>

  listRuns(workspaceId: string, cursor?: string): Promise<Page<RunSummary>>
  getRun(runId: string): Promise<Run>
  cancelRun(runId: string): Promise<void>
  /**
   * Nhận sự kiện của run. Mất kết nối không hủy run; client tự kết nối lại và
   * server phát lại từ Last-Event-ID — người gọi dedup theo `seq`.
   */
  streamRun(runId: string, onEvent: (event: RunEvent) => void, onConnection?: (state: 'open' | 'reconnecting') => void): StreamHandle
  exportRun(runId: string, format: ExportFormat): Promise<Blob>
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly retryable: boolean
  readonly traceId?: string

  constructor(status: number, body: ApiErrorBody) {
    super(body.message)
    this.status = status
    this.code = body.code
    this.retryable = body.retryable
    this.traceId = body.trace_id
  }
}

export const newIdempotencyKey = () => crypto.randomUUID()

function readCookie(name: string) {
  return document.cookie
    .split('; ')
    .find((part) => part.startsWith(`${name}=`))
    ?.slice(name.length + 1)
}

/** Client cho backend FastAPI (ARCHITECTURE.md mục 10). Phiên dùng cookie cùng origin. */
export class HttpApi implements Api {
  readonly mode = 'http' as const
  private readonly base: string

  constructor(base: string) {
    this.base = base.replace(/\/$/, '')
  }

  private async send(path: string, init: RequestInit & { idempotencyKey?: string } = {}) {
    const headers = new Headers(init.headers)
    const method = (init.method ?? 'GET').toUpperCase()
    if (method !== 'GET') {
      const csrf = readCookie('csrf_token')
      if (csrf) headers.set('X-CSRF-Token', decodeURIComponent(csrf))
    }
    if (init.idempotencyKey) headers.set('Idempotency-Key', init.idempotencyKey)
    let response: Response
    try {
      response = await fetch(`${this.base}${path}`, { ...init, headers, credentials: 'same-origin' })
    } catch {
      throw new ApiError(0, { code: 'network_error', message: 'Không kết nối được máy chủ.', retryable: true })
    }
    if (!response.ok) {
      let body: ApiErrorBody
      try {
        body = (await response.json()) as ApiErrorBody
        if (!body.message) throw new Error()
      } catch {
        body = { code: `http_${response.status}`, message: response.statusText || 'Lỗi máy chủ.', retryable: response.status >= 500 }
      }
      throw new ApiError(response.status, body)
    }
    return response
  }

  private async request<T>(path: string, init?: RequestInit & { idempotencyKey?: string }): Promise<T> {
    const response = await this.send(path, init)
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
  }

  private json<T>(path: string, method: string, body: unknown, idempotencyKey?: string) {
    return this.request<T>(path, {
      method,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
      idempotencyKey,
    })
  }

  private cursor(cursor?: string) {
    return cursor ? `?cursor=${encodeURIComponent(cursor)}` : ''
  }

  getReadiness() {
    return this.request<Readiness>('/ready')
  }

  listWorkspaces() {
    return this.request<Workspace[]>('/workspaces')
  }

  createWorkspace(name: string) {
    return this.json<Workspace>('/workspaces', 'POST', { name })
  }

  getWorkspace(id: string) {
    return this.request<Workspace>(`/workspaces/${encodeURIComponent(id)}`)
  }

  listSources(workspaceId: string, cursor?: string) {
    return this.request<Page<Source>>(`/workspaces/${encodeURIComponent(workspaceId)}/sources${this.cursor(cursor)}`)
  }

  uploadSource(workspaceId: string, file: File, idempotencyKey: string) {
    const form = new FormData()
    form.append('file', file)
    return this.request<Source>(`/workspaces/${encodeURIComponent(workspaceId)}/sources`, {
      method: 'POST',
      body: form,
      idempotencyKey,
    })
  }

  addRemoteSource(workspaceId: string, input: { url: string } | { doi: string }, idempotencyKey: string) {
    const body = 'url' in input ? { kind: 'url', url: input.url } : { kind: 'doi', doi: input.doi }
    return this.json<Source>(`/workspaces/${encodeURIComponent(workspaceId)}/sources`, 'POST', body, idempotencyKey)
  }

  deleteSource(sourceId: string) {
    return this.request<Accepted>(`/sources/${encodeURIComponent(sourceId)}`, { method: 'DELETE' })
  }

  getSourceContent(sourceId: string, parseRevisionId: string) {
    return this.request<SourceContent>(
      `/sources/${encodeURIComponent(sourceId)}/content?revision_id=${encodeURIComponent(parseRevisionId)}`,
    )
  }

  getJob(jobId: string) {
    return this.request<Job>(`/jobs/${encodeURIComponent(jobId)}`)
  }

  askQuestion(workspaceId: string, request: QaRequest, idempotencyKey: string) {
    return this.json<RunAccepted>(`/workspaces/${encodeURIComponent(workspaceId)}/qa`, 'POST', request, idempotencyKey)
  }

  proposeOutline(workspaceId: string, request: OutlineRequest) {
    return this.json<OutlineSection[]>(`/workspaces/${encodeURIComponent(workspaceId)}/synthesis/outline`, 'POST', request)
  }

  startSynthesis(workspaceId: string, request: SynthesisRequest, idempotencyKey: string) {
    return this.json<RunAccepted>(`/workspaces/${encodeURIComponent(workspaceId)}/synthesis`, 'POST', request, idempotencyKey)
  }

  listRuns(workspaceId: string, cursor?: string) {
    return this.request<Page<RunSummary>>(`/workspaces/${encodeURIComponent(workspaceId)}/runs${this.cursor(cursor)}`)
  }

  getRun(runId: string) {
    return this.request<Run>(`/runs/${encodeURIComponent(runId)}`)
  }

  cancelRun(runId: string) {
    return this.request<void>(`/runs/${encodeURIComponent(runId)}/cancel`, { method: 'POST' })
  }

  streamRun(runId: string, onEvent: (event: RunEvent) => void, onConnection?: (state: 'open' | 'reconnecting') => void) {
    // EventSource tự kết nối lại và gửi Last-Event-ID; server phát lại các event sau seq đó.
    const source = new EventSource(`${this.base}/runs/${encodeURIComponent(runId)}/stream`, { withCredentials: true })
    source.onopen = () => onConnection?.('open')
    source.onmessage = (message) => {
      const event = JSON.parse(message.data) as RunEvent
      onEvent(event)
      if (event.type === 'done') source.close()
    }
    source.onerror = () => {
      if (source.readyState === EventSource.CONNECTING) {
        onConnection?.('reconnecting')
        return
      }
      onEvent({
        seq: Number.MAX_SAFE_INTEGER,
        type: 'error',
        error: { code: 'stream_closed', message: 'Luồng sự kiện bị đóng. Tải lại trang để xem trạng thái mới nhất.', retryable: true },
      })
    }
    return { close: () => source.close() }
  }

  async exportRun(runId: string, format: ExportFormat) {
    const response = await this.send(`/runs/${encodeURIComponent(runId)}/export?format=${format}`)
    return response.blob()
  }
}
