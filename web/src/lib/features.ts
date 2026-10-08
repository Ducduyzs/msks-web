// Cờ tính năng phía giao diện. Các tính năng sau MVP (ARCHITECTURE.md mục 1.4, 16) tắt mặc định
// khi gọi backend thật; ở chế độ mock chúng bật để demo và được gắn nhãn "Thử nghiệm".

const isMock = (import.meta.env.VITE_API_MODE ?? 'mock') === 'mock'

function flag(value: string | undefined) {
  if (value === undefined || value === '') return isMock
  return value === 'true' || value === '1'
}

export const features = {
  /** G3: URL / DOI / HTML / DOCX. */
  remoteSources: flag(import.meta.env.VITE_FEATURE_REMOTE_SOURCES),
  /** G4: dàn ý + báo cáo tổng hợp, xuất DOCX/PDF. */
  synthesis: flag(import.meta.env.VITE_FEATURE_SYNTHESIS),
}

/** Định dạng tải lên của MVP (PDF có text, MD, TXT) và sau MVP. */
export const UPLOAD_ACCEPT = features.remoteSources ? '.pdf,.md,.markdown,.txt,.docx,.html,.htm' : '.pdf,.md,.markdown,.txt'

/** Giới hạn vận hành ban đầu (mục 3.2); server vẫn là nơi kiểm tra chính thức. */
export const MAX_UPLOAD_BYTES = 50 * 1024 * 1024
