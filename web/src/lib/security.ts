import { t } from '../i18n'
// Helper bảo mật phía trình duyệt. Nguyên tắc: frontend chỉ là lớp phòng thủ bổ sung — mọi quyết định truy cập
// vẫn ở backend (JWT + phiên còn sống + 2FA + RLS). Không đặt bí mật nào ở đây.

/** Sự kiện toàn cục khi API trả 401: phiên hết hạn, bị đăng xuất ở thiết bị khác hoặc cần 2FA. */
export const UNAUTHORIZED_EVENT = 'msks:unauthorized'

export function notifyUnauthorized(code: string) {
  window.dispatchEvent(new CustomEvent(UNAUTHORIZED_EVENT, { detail: code }))
}

/** Chỉ mở liên kết ngoài http(s): chặn `javascript:`, `data:`… trong URL lấy từ nguồn người dùng nhập. */
export function safeExternalUrl(url: string | null | undefined): string | null {
  if (!url) return null
  try {
    const parsed = new URL(url)
    return parsed.protocol === 'https:' || parsed.protocol === 'http:' ? parsed.href : null
  } catch {
    return null
  }
}

export const PASSWORD_MIN_LENGTH = 12

/** Kiểm tra mật khẩu khi tạo tài khoản. Supabase vẫn kiểm tra lại phía server theo cấu hình dự án. */
export function passwordProblems(password: string, email: string): string[] {
  const problems: string[] = []
  if ([...password].length < PASSWORD_MIN_LENGTH) problems.push(t('ít nhất {0} ký tự', { 0: PASSWORD_MIN_LENGTH }))
  const classes = [/[a-z]/, /[A-Z]/, /\d/, /[^A-Za-z0-9]/].filter((re) => re.test(password)).length
  if (classes < 3) problems.push(t('kết hợp ít nhất 3 loại: chữ thường, chữ hoa, số, ký tự đặc biệt'))
  const local = email.split('@')[0]?.toLowerCase() ?? ''
  if (local.length >= 3 && password.toLowerCase().includes(local)) problems.push(t('không chứa tên email'))
  if (/^(.)\1+$/.test(password) || /(?:0123|1234|2345|abcd|qwer|matkhau|password)/i.test(password)) {
    problems.push(t('không dùng chuỗi dễ đoán (1234, abcd, password…)'))
  }
  return problems
}

/** Phút không hoạt động trước khi tự đăng xuất (VITE_IDLE_LOGOUT_MINUTES, mặc định 30; 0 = tắt). */
export function idleLogoutMs() {
  const minutes = Number(import.meta.env.VITE_IDLE_LOGOUT_MINUTES ?? 30)
  return Number.isFinite(minutes) && minutes > 0 ? minutes * 60_000 : 0
}

export { buildCsp, securityHeaders } from './csp'
