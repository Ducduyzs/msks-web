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
  EvidenceRef,
  ExportFormat,
  Job,
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
  SynthesisRequest,
  Workspace,
} from './types'
import { utf16ToCp } from '../lib/codepoints'

// ------------------------------------------------------------------ helpers

interface StoredSource {
  source: Source
  content: SourceContent | null
  purged: boolean
}

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

  // ------------------------------------------------------------- run helpers

  private scope(workspaceId: string, sourceIds: string[]) {
    const scope = this.visibleSources(workspaceId).filter(
      (s) => s.source.status === 'ready' && !s.source.deleted_at && (sourceIds.length === 0 || sourceIds.includes(s.source.id)),
    )
    if (!scope.length) throw err(422, 'empty_scope', 'Phạm vi không có nguồn nào sẵn sàng.')
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
    const context: ContextBlock[] = claims.map((claim, index) => {
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
      primary.context_block_id = block.id
      return block
    })
    const rejected: RejectedWithSupport[] = claims.length
      ? SEED_REJECTED.filter((r) => !sectionKeys || sectionKeys.includes(r.section_key)).map((r) => ({ ...r, id: `${r.id}_${runId}` }))
      : []
    return { claims, context, rejected }
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
      profile: 'product',
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
      let plan = this.plan(runId, scope, request.budget)
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
