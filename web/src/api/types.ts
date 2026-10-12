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
  /** Thuật ngữ đưa vào initial_prompt của ASR cho video tải lên sau đó (LECTURE mục 5.2). */
  asr_glossary?: string[]
}

// ------------------------------------------------------------------ nguồn

export type SourceKind = 'pdf' | 'docx' | 'html' | 'markdown' | 'txt' | 'doi' | 'video' | 'youtube' | 'drive'
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
  /** Bài giảng thiếu nhánh tùy chọn (không có hình, chỉ có phụ đề…) — vẫn hỏi được (LECTURE mục 8). */
  | 'ready_limited'
  /** Bản chép có quá nhiều đoạn bị cảnh báo — nên xem lại trước khi tin (LECTURE mục 8). */
  | 'review_required'
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
  // Bài giảng
  language?: Language
  duration_ms?: number
  capabilities?: MediaCapabilities
  coverage?: MediaCoverage
}

export type Language = 'vi' | 'en' | 'auto'

export interface MediaCapabilities {
  audio_available: boolean | null
  visual_available: boolean | null
  captions_available: boolean | null
  /** 'kept' = giữ video gốc; 'audio_and_frames' = chỉ còn âm thanh + ảnh khung hình; 'external' = phát trên YouTube. */
  original_playback_available: 'kept' | 'audio_and_frames' | 'external' | null
}

/** Độ phủ thực tế của lần trích xuất (LECTURE mục 8, media/timeline.coverage). */
export interface MediaCoverage {
  duration_ms?: number | null
  speech_ratio?: number | null
  segments?: number
  segments_flagged?: number
  flagged_ratio?: number | null
  segments_dropped_as_non_speech?: number
  frames_selected?: number
  board_regions?: number
  board_regions_low_confidence?: number
  visual_incomplete_from_ms?: number | null
  audio_read?: boolean
  captions_used?: boolean
  visual_read?: boolean
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

export type JobKind = 'ingest' | 'delete' | 'reindex' | 'run'
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
export type RunProfile = 'product' | 'v11_parity' | 'lecture_vi_v1'
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
  /** Bài giảng: evidence đến từ lời giảng, phụ đề hay bảng/slide. Tài liệu: 'text'. */
  modality?: Modality
  /** Vị trí trong media; null khi nguồn đã xóa hoặc evidence là văn bản. */
  locator?: MediaLocator | null
  transcription_state?: 'automatic' | 'reviewed' | 'not_applicable'
}

export type Modality = 'text' | 'speech' | 'caption' | 'board'

export interface LocatorItem {
  type: 'segment' | 'region'
  id: string
  start_ms: number
  end_ms: number
  asset_id: string | null
  bbox: [number, number, number, number] | null
  flags: string[]
  reviewed: boolean
}

export interface MediaLocator {
  modality: Modality
  start_ms?: number
  end_ms?: number
  precision: 'segment' | 'region' | 'mixed' | 'none'
  items: LocatorItem[]
  extraction_revision_id?: string | null
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
  /** Nhãn trung thực: bản chép máy ≠ văn bản gốc ≠ bản đã đối chiếu (LECTURE mục 7). */
  support_basis?: 'document' | 'automatic_transcript' | 'reviewed_transcript'
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
  modality?: Modality
  start_ms?: number | null
  end_ms?: number | null
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

// ---------------------------------------------------------------- bài giảng

export type UploadSessionStatus = 'open' | 'completed' | 'cancelled' | 'expired'

export interface UploadPart {
  index: number
  size: number
  uploaded: boolean
  /** URL ký sẵn (PUT) cho phần còn thiếu; hạn ~2 giờ — lấy lại bằng getMediaSession. */
  upload_url?: string
  method?: 'PUT'
  /** Phần đã tải sai kích thước: không ghi đè được, phải hủy phiên. */
  error?: 'size_mismatch'
}

export interface MediaUploadSession {
  id: string
  workspace_id: string
  status: UploadSessionStatus
  filename: string
  mime: string
  expected_bytes: number
  part_bytes: number
  part_count: number
  language: Language
  keep_original: boolean
  caption: { filename: string } | null
  parts: UploadPart[]
}

export interface MediaSessionRequest {
  filename: string
  size_bytes: number
  mime: string
  language: Language
  keep_original: boolean
}

export interface TranscriptSegment {
  id: string
  start_ms: number
  end_ms: number
  text: string
  origin: 'asr' | 'human_caption' | 'manual_edit' | string
  flags: string[]
  reviewed: boolean
  audio_asset_id: string | null
}

export interface FrameRegion {
  id: string
  frame_asset_id: string
  timestamp_ms: number
  visible_from_ms: number
  visible_to_ms: number
  /** [x0, y0, x1, y1] theo pixel của frame gốc. */
  bbox: [number, number, number, number]
  text: string
  method: string
  confidence: number | null
  included: boolean
  flags: string[]
  review_state: string
}

export interface MediaAssetRef {
  id: string
  kind: 'audio' | 'frame'
  mime: string
  meta: { start_ms?: number; end_ms?: number; chunk_index?: number; timestamp_ms?: number; width?: number; height?: number; scale?: number }
}

export interface Timeline {
  source_id: string
  parse_revision_id: string
  extraction_revision_id: string
  parent_extraction_revision_id: string | null
  duration_ms: number | null
  capabilities: MediaCapabilities | null
  coverage: MediaCoverage | null
  versions: { asr?: { model?: string; glossary_sha256?: string | null } | null; ocr?: { engine?: string } } & Record<string, unknown>
  segments: TranscriptSegment[]
  regions: FrameRegion[]
  assets: MediaAssetRef[]
}

export interface MediaUrl {
  asset_id: string
  kind: string
  mime: string
  url: string
  expires_in: number
}

export interface TranscriptEdit {
  base_revision_id: string
  segments: { id: string; text: string }[]
  regions: { id: string; text: string }[]
}

// ------------------------------------------------------------- dịch tài liệu

export interface TranslationLanguage {
  code: string
  name: string
}

export type TranslationStatus = 'queued' | 'running' | 'succeeded' | 'failed'

/** Bản dịch máy của một parse revision — chỉ để đọc; trích dẫn và kiểm chứng luôn dựa trên bản gốc. */
export interface SourceTranslation {
  id: string
  source_id: string
  parse_revision_id: string
  target_lang: string
  status: TranslationStatus
  progress: number
  segment_count: number
  char_count: number
  /** Số đoạn có cảnh báo (vd. số liệu khác bản gốc). */
  flagged_count: number
  model: string | null
  error: ApiErrorBody | null
  created_at: string
  finished_at: string | null
}

export interface TranslationSegment {
  node_id: string
  ordinal: number
  original: string
  text: string
  /** 'numbers_changed' | 'empty' */
  flags: string[]
  char_start: number
  char_end: number
  page_start: number | null
  page_end: number | null
  modality: Modality
  start_ms: number | null
  end_ms: number | null
}

export interface TranslationDetail extends SourceTranslation {
  segments: TranslationSegment[]
}
