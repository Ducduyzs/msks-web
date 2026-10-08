// Hợp đồng dữ liệu với backend — ARCHITECTURE.md v0.2, mục 4.3, 5, 7, 8, 10.
// Offset ký tự (char_*, quote_*) là khoảng nửa mở [start, end) theo Unicode code point
// trong canonical text của parse revision (mục 5.4); xem lib/codepoints.ts.

// ------------------------------------------------------------------ chung

/** Thân lỗi chuẩn của API (mục 10). */
export interface ApiErrorBody {
  code: string
  message: string
  retryable: boolean
  trace_id?: string
}

/** Danh sách phân trang bằng cursor. */
export interface Page<T> {
  items: T[]
  next_cursor: string | null
}

export interface Accepted {
  job_id: string
}

export interface Readiness {
  ready: boolean
  components: Record<string, 'ok' | 'degraded' | 'down'>
  /** Nhà cung cấp LLM đang cấu hình — UI phải báo khi đoạn trích được gửi ra ngoài (mục 15). */
  llm_provider: string | null
  /** Ngưỡng NLI chưa có calibration artifact → kết quả chỉ là thử nghiệm (mục 8.1, 13). */
  calibrated: boolean
}

// --------------------------------------------------------------- workspace

export interface Workspace {
  id: string
  name: string
  created_at: string
  source_count: number
  run_count: number
}

// ------------------------------------------------------------------ nguồn

export type SourceKind = 'pdf' | 'docx' | 'html' | 'markdown' | 'txt' | 'doi'
/** Loại nguồn — mô tả, không phải điểm tin cậy (mục 5.2). */
export type SourceType = 'peer_reviewed' | 'preprint' | 'web' | 'user_note'
/** Vòng đời document (mục 6.5) + trạng thái xóa (tombstone). */
export type SourceStatus =
  | 'uploaded'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'publishing'
  | 'ready'
  | 'failed'
  | 'cancelled'
  | 'deleting'
export type IndependenceStatus = 'unknown' | 'reviewed'

export interface SourceRevision {
  document_revision_id: string
  revision_no: number
  parse_revision_id: string
  /** null với định dạng không phân trang (HTML, Markdown, TXT). */
  page_count: number | null
  leaf_count: number
}

export interface Source {
  id: string
  workspace_id: string
  alias: string
  kind: SourceKind
  title: string
  authors: string[]
  url?: string
  doi?: string
  published_at?: string
  source_type: SourceType
  origin_group_id: string
  independence_status: IndependenceStatus
  grouping_version: number
  grouping_reason?: string
  status: SourceStatus
  progress: number
  job_id?: string
  error?: ApiErrorBody
  revision?: SourceRevision
  deleted_at?: string
  created_at: string
}

export interface PageSpan {
  page: number
  char_start: number
  char_end: number
}

export interface SourceContent {
  source_id: string
  parse_revision_id: string
  text: string
  /** null nếu định dạng không phân trang. */
  pages: PageSpan[] | null
}

export type JobKind = 'ingest' | 'delete'
export type JobState = 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'

export interface Job {
  id: string
  kind: JobKind
  target_id: string
  state: JobState
  stage?: string
  retry_count: number
  error?: ApiErrorBody
}

// --------------------------------------------------------------------- run

export type RunMode = 'qa' | 'synthesis'
export type RunProfile = 'product' | 'v11_parity'
export type RunStatus = 'queued' | 'running' | 'succeeded' | 'partial' | 'failed' | 'cancelled'
export type AnswerStatus = 'answered' | 'insufficient_evidence' | 'model_output_invalid' | 'verification_unavailable'
export type QueryType = 'factoid' | 'explanatory' | 'comparative_multi_hop' | 'global_synthesis'
export type Budget = 512 | 1024 | 2048
export const BUDGETS: Budget[] = [512, 1024, 2048]

/** Trạng thái claim đã publish. PENDING/REJECTED chỉ có trong audit (mục 5.3). */
export type ClaimStatus = 'supported' | 'corroborated' | 'contested'
export type CrossCheckStatus = 'not_run' | 'completed' | 'incomplete'
export type EvidenceRole = 'primary' | 'corroborating' | 'contradicting'

export interface EvidenceRef {
  id: string
  role: EvidenceRole
  source_id: string
  source_alias: string
  node_id: string
  parse_revision_id: string
  /** Bắt buộc với evidence chính (mục 5.2). */
  context_block_id?: string
  page_start: number | null
  page_end: number | null
  char_start: number
  char_end: number
  quote_start: number
  quote_end: number
  /** Đoạn evidence đã kiểm chứng; rỗng khi nguồn đã bị xóa. */
  evidence_snapshot: string
  /** Tín hiệu model, không phải xác suất đúng (mục 8.1). Chỉ hiện trong audit. */
  support: number
  contradiction: number
  verifier_revision: string
  /** Có ánh xạ vị trí đáng tin vào canonical text hay không (mục 5.4). */
  position: 'exact' | 'approximate' | 'unmapped'
  source_deleted?: boolean
}

export interface ClaimResult {
  id: string
  text: string
  section_key?: string
  status: ClaimStatus
  cross_check_status: CrossCheckStatus
  verification_method: string
  /** Tự đánh giá của LLM — không phải xác suất đúng. */
  generator_confidence: number
  evidence: EvidenceRef[]
}

export interface RejectedClaim {
  id: string
  text: string
  reason: string
  section_key?: string
  support?: number | null
}

export interface ContextBlock {
  id: string
  context_id: string
  node_id: string
  source_alias: string
  parse_revision_id: string
  page_start: number | null
  page_end: number | null
  rank: number
  visible_text: string
  token_count: number
  truncated: boolean
  /** Các điểm lưu riêng — điểm gán lại của Agreement không phải xác suất hỗ trợ (mục 7.2). */
  trace: {
    raw_ce_score: number
    sbert_similarity: number
    rrf_score: number
    packing_utility: number
  }
}

/** Chỉ số mục 8.4; `null` khi mẫu số bằng 0. */
export interface RunMetrics {
  generated_count: number
  accepted_count: number
  json_error_count: number
  primary_attribution_risk_generated: number | null
  primary_attribution_risk_accepted: number | null
  unsupported_generated_rate: number | null
  citation_survival_rate: number | null
  corroboration_rate: number | null
  contest_rate: number | null
  source_concentration: number | null
  origin_groups_cited: number
  cross_check_completion_rate: number | null
  context_tokens: number
  latency_ms: Partial<Record<'queue' | 'retrieval' | 'rerank' | 'generation' | 'verification' | 'cross_check' | 'total', number>>
}

export interface SourceSnapshot {
  source_id: string
  alias: string
  document_revision_id: string
  parse_revision_id: string
}

export interface Provenance {
  profile: RunProfile
  config_hash: string
  code_hash: string
  edahr_commit: string | null
  verifier_revision: string
  calibration_artifact: string | null
  grouping_version: number
  models: Record<string, string>
  source_snapshot: SourceSnapshot[]
}

export interface OutlineSection {
  key: string
  title: string
  questions: string[]
}

export interface ReportParagraph {
  /** Câu dẫn cố định từ template — không chứa sự kiện (mục 7.3). */
  template_text: string
  claim_ids: string[]
}

export interface ReportSection {
  key: string
  title: string
  status: 'complete' | 'insufficient_evidence' | 'failed'
  paragraphs: ReportParagraph[]
}

export interface Run {
  run_id: string
  workspace_id: string
  parent_run_id?: string
  rerun_of?: string
  mode: RunMode
  profile: RunProfile
  query: string
  query_type?: QueryType
  budget: { context_tokens: Budget }
  source_ids: string[]
  status: RunStatus
  answer_status?: AnswerStatus
  cancel_requested: boolean
  /** Lý do khi PARTIAL / FAILED. */
  error?: ApiErrorBody
  claims: ClaimResult[]
  rejected_claims: RejectedClaim[]
  context: ContextBlock[]
  outline?: OutlineSection[]
  sections?: ReportSection[]
  metrics?: RunMetrics
  provenance?: Provenance
  created_at: string
  finished_at?: string
}

export type RunSummary = Pick<
  Run,
  'run_id' | 'mode' | 'query' | 'status' | 'answer_status' | 'created_at' | 'budget' | 'rerun_of'
> & { accepted_count: number }

export interface QaRequest {
  question: string
  source_ids: string[]
  budget: Budget
  mode: 'standard'
  rerun_of?: string
}

export interface OutlineRequest {
  topic: string
  source_ids: string[]
}

export interface SynthesisRequest {
  topic: string
  source_ids: string[]
  outline: OutlineSection[]
  budget: Budget
}

export interface RunAccepted {
  run_id: string
  status_url: string
  stream_url: string
}

export type RunStage =
  | 'queued'
  | 'classify'
  | 'retrieve'
  | 'rank'
  | 'pack'
  | 'generate'
  | 'verify'
  | 'cross_check'
  | 'outline'
  | 'subquestion'
  | 'compose'

/** Sự kiện SSE của GET /api/runs/{id}/stream; `seq` tăng dần, dùng để dedup khi phát lại. */
export type RunEvent =
  | { seq: number; type: 'stage'; stage: RunStage; label: string }
  | { seq: number; type: 'claim_verified'; claim: ClaimResult }
  | { seq: number; type: 'section'; section: ReportSection }
  | { seq: number; type: 'done'; status: RunStatus }
  | { seq: number; type: 'error'; error: ApiErrorBody }

export type ExportFormat = 'md' | 'json' | 'docx' | 'pdf'
