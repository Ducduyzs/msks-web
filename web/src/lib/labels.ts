import type {
  AnswerStatus,
  ClaimStatus,
  CrossCheckStatus,
  EvidenceRole,
  Language,
  Modality,
  QueryType,
  RunStatus,
  SourceKind,
  SourceStatus,
  SourceType,
} from '../api'
import { msg, translated, currentLocale } from '../i18n'

// Nhãn theo ARCHITECTURE.md mục 11: không ngụ ý "đã chứng minh là đúng".
export const CLAIM_STATUS: Record<ClaimStatus, { label: string; hint: string }> = translated({
  supported: {
    label: msg('Được bằng chứng hỗ trợ'),
    hint: msg('Evidence chính đạt policy kiểm chứng của profile. Không đồng nghĩa sự thật đã được chứng minh.'),
  },
  corroborated: {
    label: msg('Có đối chứng độc lập'),
    hint: msg('Một nhóm nguồn khác — đã được đánh giá là độc lập — cũng hỗ trợ, và không tìm thấy mâu thuẫn trong phạm vi đã xét.'),
  },
  contested: {
    label: msg('Có bằng chứng mâu thuẫn'),
    hint: msg('Một nguồn khác cùng bối cảnh có đoạn mâu thuẫn. Xem cả hai phía trước khi dùng.'),
  },
})

export const CROSS_CHECK: Record<CrossCheckStatus, string> = translated({
  not_run: msg('Chưa đối chứng chéo'),
  completed: msg('Đã đối chứng trong phạm vi nguồn đã chọn'),
  incomplete: msg('Đối chứng chưa hoàn tất (hết ngân sách tìm kiếm)'),
})

export const ANSWER_STATUS: Record<AnswerStatus, { label: string; hint: string }> = translated({
  answered: { label: msg('Đã trả lời'), hint: msg('Có ít nhất một claim qua kiểm chứng.') },
  insufficient_evidence: {
    label: msg('Không đủ bằng chứng'),
    hint: msg('Không claim nào qua kiểm chứng trong phạm vi nguồn đã chọn. Hệ thống không trả lời thay vì đoán.'),
  },
  model_output_invalid: {
    label: msg('Mô hình trả về sai định dạng'),
    hint: msg('Đầu ra của LLM vi phạm hợp đồng JSON/citation; lượt này không có claim.'),
  },
  verification_unavailable: {
    label: msg('Không kiểm chứng được'),
    hint: msg('Dịch vụ kiểm chứng lỗi hoặc quá hạn; claim không được mặc định là đạt.'),
  },
})

export const RUN_STATUS: Record<RunStatus, string> = translated({
  queued: msg('Đang chờ'),
  running: msg('Đang chạy'),
  succeeded: msg('Hoàn tất'),
  partial: msg('Hoàn tất một phần'),
  failed: msg('Thất bại'),
  cancelled: msg('Đã hủy'),
})

export const SOURCE_STATUS: Record<SourceStatus, string> = translated({
  uploaded: msg('Đã tải lên'),
  parsing: msg('Đang phân tích'),
  chunking: msg('Đang chia đoạn'),
  embedding: msg('Đang mã hóa'),
  publishing: msg('Đang công bố chỉ mục'),
  ready: msg('Sẵn sàng'),
  ready_limited: msg('Sẵn sàng (giới hạn)'),
  review_required: msg('Cần xem lại bản chép'),
  failed: msg('Lỗi'),
  cancelled: msg('Đã hủy'),
  deleting: msg('Đã ẩn · đang xóa dữ liệu'),
})

export const SOURCE_KIND: Record<SourceKind, string> = translated({
  pdf: 'PDF',
  docx: 'DOCX',
  html: 'Web',
  markdown: 'Markdown',
  txt: msg('Văn bản'),
  doi: 'DOI',
  video: msg('Video bài giảng'),
  youtube: 'YouTube',
  drive: 'Google Drive',
})

/** Gợi ý cho trạng thái bài giảng (LECTURE_ARCHITECTURE.md mục 8). */
export const SOURCE_STATUS_HINT: Partial<Record<SourceStatus, string>> = translated({
  ready_limited: msg('Hỏi được, nhưng thiếu một nhánh: không có hình ảnh (chỉ phụ đề), không có âm thanh, hoặc chưa đọc hết khung hình.'),
  review_required: msg('Nhiều đoạn bản chép bị cảnh báo (độ tin thấp, lặp, lệch thời gian). Hỏi được, nhưng claim có số liệu trên các đoạn đó bị loại cho đến khi bạn sửa bản chép.'),
})

export const LANGUAGE: Record<Language, string> = translated({ vi: msg('Tiếng Việt'), en: msg('Tiếng Anh'), auto: msg('Tự nhận diện') })

export const MODALITY: Record<Modality, string> = translated({
  text: msg('Văn bản'),
  speech: msg('Lời giảng'),
  caption: msg('Phụ đề'),
  board: msg('Bảng / slide'),
})

/** Cờ chất lượng của đoạn bản chép / vùng chữ (media/asr.py, captions.py, timeline.py). */
export const QUALITY_FLAG: Record<string, string> = translated({
  low_confidence: msg('Độ tin thấp'),
  possible_repetition: msg('Có thể bị lặp'),
  possible_non_speech: msg('Có thể không phải lời nói'),
  beyond_media_end: msg('Vượt quá cuối video'),
  stock_phrase: msg('Có câu ASR hay bịa'),
  alignment_uncertain: msg('Thời gian chưa chắc'),
  overlapping_cue: msg('Phụ đề chồng lấn'),
  caption_invalid: msg('Phụ đề lỗi'),
})

export const qualityFlag = (flag: string) => QUALITY_FLAG[flag] ?? flag

export const SUPPORT_BASIS: Record<'document' | 'automatic_transcript' | 'reviewed_transcript', { label: string; hint: string } | null> = translated({
  document: null,
  automatic_transcript: {
    label: msg('Được bản chép tự động hỗ trợ'),
    hint: msg('Bằng chứng là bản chép lời/OCR do máy tạo, chưa được người đối chiếu với âm thanh/hình ảnh. Bản chép có thể sai từ hoặc số.'),
  },
  reviewed_transcript: {
    label: msg('Bản chép đã được sửa/đối chiếu'),
    hint: msg('Đoạn bằng chứng đã được người dùng sửa hoặc xác nhận.'),
  },
})

export const isLecture = (kind: SourceKind) => kind === 'video' || kind === 'youtube' || kind === 'drive'

export const SOURCE_TYPE: Record<SourceType, string> = translated({
  peer_reviewed: msg('Bài đã bình duyệt'),
  preprint: 'Preprint',
  web: 'Trang web',
  user_note: msg('Ghi chú người dùng'),
})

export const QUERY_TYPE: Record<QueryType, string> = translated({
  factoid: msg('Sự kiện'),
  explanatory: msg('Giải thích'),
  comparative_multi_hop: msg('So sánh'),
  global_synthesis: msg('Tổng quan'),
})

export const EVIDENCE_ROLE: Record<EvidenceRole, string> = translated({
  primary: msg('Bằng chứng chính'),
  corroborating: msg('Nguồn khác hỗ trợ'),
  contradicting: msg('Nguồn khác mâu thuẫn'),
})

// Lý do loại claim, theo trạng thái trong edahr.verification và mục 8.1.
export const REJECT_REASON: Record<string, string> = translated({
  invalid_or_missing_context_citation: msg('Không có trích dẫn hợp lệ'),
  below_claim_confidence_threshold: msg('Tự đánh giá của mô hình dưới ngưỡng'),
  no_reachable_child: msg('Không có đoạn lá nào trong khối được trích'),
  below_support_threshold: msg('Evidence không đạt ngưỡng hỗ trợ'),
  contradicted_by_evidence: msg('Evidence được trích mâu thuẫn với claim'),
  verification_unavailable: msg('Không kiểm chứng được (dịch vụ lỗi)'),
  local_invalid_json: msg('Mô hình trả về sai định dạng JSON'),
  number_not_in_evidence: msg('Claim có con số không có trong bằng chứng'),
  non_inferiority_overclaimed: msg('Bằng chứng chỉ nói “không kém”, claim lại nói “tốt hơn”'),
  extraction_quality_review_required: msg('Số liệu dựa trên đoạn bản chép bị cảnh báo — cần sửa bản chép trước'),
})

export const rejectReason = (reason: string) => REJECT_REASON[reason] ?? reason

export const pct = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`)
export const num = (value: number | null, digits = 2) => (value === null ? '—' : value.toFixed(digits))

export function fmtDate(iso: string) {
  return new Date(iso).toLocaleString(currentLocale(), { dateStyle: 'short', timeStyle: 'short' })
}

export function pageLabel(start: number | null, end: number | null) {
  if (start === null) return null
  return end !== null && end !== start ? `tr. ${start}–${end}` : `tr. ${start}`
}

export const isTerminal = (status: RunStatus) => ['succeeded', 'partial', 'failed', 'cancelled'].includes(status)

export const isIngesting = (status: SourceStatus) => ['uploaded', 'parsing', 'chunking', 'embedding', 'publishing'].includes(status)

/** Nguồn hỏi được: tài liệu `ready`; bài giảng cũng hỏi được khi `ready_limited` / `review_required`. */
export const isQueryable = (status: SourceStatus) => ['ready', 'ready_limited', 'review_required'].includes(status)
