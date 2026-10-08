import type {
  AnswerStatus,
  ClaimStatus,
  CrossCheckStatus,
  EvidenceRole,
  QueryType,
  RunStatus,
  SourceKind,
  SourceStatus,
  SourceType,
} from '../api'

// Nhãn theo ARCHITECTURE.md mục 11: không ngụ ý "đã chứng minh là đúng".
export const CLAIM_STATUS: Record<ClaimStatus, { label: string; hint: string }> = {
  supported: {
    label: 'Được bằng chứng hỗ trợ',
    hint: 'Evidence chính đạt policy kiểm chứng của profile. Không đồng nghĩa sự thật đã được chứng minh.',
  },
  corroborated: {
    label: 'Có đối chứng độc lập',
    hint: 'Một nhóm nguồn khác — đã được đánh giá là độc lập — cũng hỗ trợ, và không tìm thấy mâu thuẫn trong phạm vi đã xét.',
  },
  contested: {
    label: 'Có bằng chứng mâu thuẫn',
    hint: 'Một nguồn khác cùng bối cảnh có đoạn mâu thuẫn. Xem cả hai phía trước khi dùng.',
  },
}

export const CROSS_CHECK: Record<CrossCheckStatus, string> = {
  not_run: 'Chưa đối chứng chéo',
  completed: 'Đã đối chứng trong phạm vi nguồn đã chọn',
  incomplete: 'Đối chứng chưa hoàn tất (hết ngân sách tìm kiếm)',
}

export const ANSWER_STATUS: Record<AnswerStatus, { label: string; hint: string }> = {
  answered: { label: 'Đã trả lời', hint: 'Có ít nhất một claim qua kiểm chứng.' },
  insufficient_evidence: {
    label: 'Không đủ bằng chứng',
    hint: 'Không claim nào qua kiểm chứng trong phạm vi nguồn đã chọn. Hệ thống không trả lời thay vì đoán.',
  },
  model_output_invalid: {
    label: 'Mô hình trả về sai định dạng',
    hint: 'Đầu ra của LLM vi phạm hợp đồng JSON/citation; lượt này không có claim.',
  },
  verification_unavailable: {
    label: 'Không kiểm chứng được',
    hint: 'Dịch vụ kiểm chứng lỗi hoặc quá hạn; claim không được mặc định là đạt.',
  },
}

export const RUN_STATUS: Record<RunStatus, string> = {
  queued: 'Đang chờ',
  running: 'Đang chạy',
  succeeded: 'Hoàn tất',
  partial: 'Hoàn tất một phần',
  failed: 'Thất bại',
  cancelled: 'Đã hủy',
}

export const SOURCE_STATUS: Record<SourceStatus, string> = {
  uploaded: 'Đã tải lên',
  parsing: 'Đang phân tích',
  chunking: 'Đang chia đoạn',
  embedding: 'Đang mã hóa',
  publishing: 'Đang công bố chỉ mục',
  ready: 'Sẵn sàng',
  failed: 'Lỗi',
  cancelled: 'Đã hủy',
  deleting: 'Đã ẩn · đang xóa dữ liệu',
}

export const SOURCE_KIND: Record<SourceKind, string> = {
  pdf: 'PDF',
  docx: 'DOCX',
  html: 'Web',
  markdown: 'Markdown',
  txt: 'Văn bản',
  doi: 'DOI',
}

export const SOURCE_TYPE: Record<SourceType, string> = {
  peer_reviewed: 'Bài đã bình duyệt',
  preprint: 'Preprint',
  web: 'Trang web',
  user_note: 'Ghi chú người dùng',
}

export const QUERY_TYPE: Record<QueryType, string> = {
  factoid: 'Sự kiện',
  explanatory: 'Giải thích',
  comparative_multi_hop: 'So sánh',
  global_synthesis: 'Tổng quan',
}

export const EVIDENCE_ROLE: Record<EvidenceRole, string> = {
  primary: 'Bằng chứng chính',
  corroborating: 'Nguồn khác hỗ trợ',
  contradicting: 'Nguồn khác mâu thuẫn',
}

// Lý do loại claim, theo trạng thái trong edahr.verification và mục 8.1.
export const REJECT_REASON: Record<string, string> = {
  invalid_or_missing_context_citation: 'Không có trích dẫn hợp lệ',
  below_claim_confidence_threshold: 'Tự đánh giá của mô hình dưới ngưỡng',
  no_reachable_child: 'Không có đoạn lá nào trong khối được trích',
  below_support_threshold: 'Evidence không đạt ngưỡng hỗ trợ',
  contradicted_by_evidence: 'Evidence được trích mâu thuẫn với claim',
  verification_unavailable: 'Không kiểm chứng được (dịch vụ lỗi)',
  local_invalid_json: 'Mô hình trả về sai định dạng JSON',
}

export const rejectReason = (reason: string) => REJECT_REASON[reason] ?? reason

export const pct = (value: number | null) => (value === null ? '—' : `${Math.round(value * 100)}%`)
export const num = (value: number | null, digits = 2) => (value === null ? '—' : value.toFixed(digits))

export function fmtDate(iso: string) {
  return new Date(iso).toLocaleString('vi-VN', { dateStyle: 'short', timeStyle: 'short' })
}

export function pageLabel(start: number | null, end: number | null) {
  if (start === null) return null
  return end !== null && end !== start ? `tr. ${start}–${end}` : `tr. ${start}`
}

export const isTerminal = (status: RunStatus) => ['succeeded', 'partial', 'failed', 'cancelled'].includes(status)

export const isIngesting = (status: SourceStatus) => ['uploaded', 'parsing', 'chunking', 'embedding', 'publishing'].includes(status)
