// Đa ngôn ngữ cho giao diện, không thư viện ngoài. Khóa là chính câu tiếng Việt (kiểu gettext): code vẫn đọc được,
// thiếu bản dịch thì hiện tiếng Việt thay vì khóa vô nghĩa. Từ điển mỗi ngôn ngữ (locales/<mã>.json) được tạo bởi
// `npm run i18n:extract` + `python scripts/translate_ui.py` và chỉ tải khi người dùng chọn ngôn ngữ đó.
import { useSyncExternalStore } from 'react'

export interface LanguageInfo {
  code: string
  /** Tên bằng chính ngôn ngữ đó — người không đọc được tiếng Việt vẫn tìm ra. */
  name: string
  /** Locale cho Intl (ngày giờ, số). */
  locale: string
}

export const LANGUAGES: LanguageInfo[] = [
  { code: 'vi', name: 'Tiếng Việt', locale: 'vi-VN' },
  { code: 'en', name: 'English', locale: 'en-US' },
  { code: 'zh', name: '简体中文', locale: 'zh-CN' },
  { code: 'ja', name: '日本語', locale: 'ja-JP' },
  { code: 'ko', name: '한국어', locale: 'ko-KR' },
  { code: 'fr', name: 'Français', locale: 'fr-FR' },
  { code: 'de', name: 'Deutsch', locale: 'de-DE' },
  { code: 'es', name: 'Español', locale: 'es-ES' },
]
export const SOURCE_LANGUAGE = 'vi'

type Dict = Record<string, string>
const loaders = import.meta.glob<{ default: Dict }>('./locales/*.json')
const KEY = 'msks.language'
const listeners = new Set<() => void>()
let language = SOURCE_LANGUAGE
let dict: Dict = {}

/** Dịch một câu giao diện; `{tên}` được thay bằng giá trị trong `vars`. */
export function t(message: string, vars?: Record<string, unknown>): string {
  const text = dict[message] ?? message
  return vars ? text.replace(/\{(\w+)\}/g, (match, name: string) => (name in vars ? String(vars[name]) : match)) : text
}

/** Đánh dấu câu ở mức module (bảng nhãn) để công cụ trích xuất thấy; dịch lúc hiển thị bằng t() hoặc translated(). */
export const msg = (message: string) => message

/**
 * Bảng nhãn khai báo một lần ở mức module nhưng dịch lúc đọc: mọi giá trị chuỗi (kể cả lồng trong object) đi qua t().
 * Vì cả ứng dụng render lại khi đổi ngôn ngữ (LanguageRoot), mọi chỗ đọc bảng đều nhận ngôn ngữ mới.
 */
export function translated<T extends object>(table: T): T {
  return new Proxy(table, {
    get(target, key, receiver) {
      const value = Reflect.get(target, key, receiver)
      if (typeof value === 'string') return t(value)
      if (value && typeof value === 'object') return translated(value)
      return value
    },
  })
}

export function currentLanguage() {
  return language
}

export function currentLocale() {
  return LANGUAGES.find((l) => l.code === language)?.locale ?? 'vi-VN'
}

export async function setLanguage(code: string) {
  const info = LANGUAGES.find((l) => l.code === code) ?? LANGUAGES[0]
  let next: Dict = {}
  if (info.code !== SOURCE_LANGUAGE) {
    const load = loaders[`./locales/${info.code}.json`]
    if (load) next = (await load()).default
  }
  dict = next
  language = info.code
  document.documentElement.lang = info.code
  try {
    localStorage.setItem(KEY, info.code)
  } catch {
    /* không lưu được: chỉ áp cho phiên này */
  }
  listeners.forEach((l) => l())
}

/** Ngôn ngữ đã chọn trước đó, hoặc ngôn ngữ trình duyệt nếu có bản dịch, hoặc tiếng Việt. */
export function initialLanguage(): string {
  try {
    const saved = localStorage.getItem(KEY)
    if (saved && LANGUAGES.some((l) => l.code === saved)) return saved
  } catch {
    /* bỏ qua */
  }
  const browser = (navigator.language || '').toLowerCase().split('-')[0]
  return LANGUAGES.some((l) => l.code === browser) ? browser : SOURCE_LANGUAGE
}

export function useLanguage() {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => language,
  )
}
