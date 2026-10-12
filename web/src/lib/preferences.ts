// Tùy chọn giao diện của từng người xem: chế độ sáng/tối, màu nhấn, cỡ chữ. Lưu trong localStorage (chỉ trình duyệt
// này) và áp vào <html data-*> trước khi React render để không chớp màu. Ngôn ngữ nằm ở i18n.
import { useSyncExternalStore } from 'react'

export type ThemeMode = 'system' | 'light' | 'dark'
export type Accent = 'blue' | 'indigo' | 'violet' | 'teal' | 'rose' | 'slate'
export type FontSize = 'small' | 'normal' | 'large'

export interface Preferences {
  theme: ThemeMode
  accent: Accent
  font: FontSize
}

export const ACCENTS: { id: Accent; swatch: string }[] = [
  { id: 'blue', swatch: '#2f5bd3' },
  { id: 'indigo', swatch: '#4f46e5' },
  { id: 'violet', swatch: '#7c3aed' },
  { id: 'teal', swatch: '#0f766e' },
  { id: 'rose', swatch: '#be185d' },
  { id: 'slate', swatch: '#475569' },
]

const KEY = 'msks.preferences'
const DEFAULTS: Preferences = { theme: 'system', accent: 'blue', font: 'normal' }
const listeners = new Set<() => void>()
let current: Preferences = load()

function load(): Preferences {
  try {
    const raw = JSON.parse(localStorage.getItem(KEY) ?? '{}') as Partial<Preferences>
    return {
      theme: ['system', 'light', 'dark'].includes(raw.theme as string) ? (raw.theme as ThemeMode) : DEFAULTS.theme,
      accent: ACCENTS.some((a) => a.id === raw.accent) ? (raw.accent as Accent) : DEFAULTS.accent,
      font: ['small', 'normal', 'large'].includes(raw.font as string) ? (raw.font as FontSize) : DEFAULTS.font,
    }
  } catch {
    return { ...DEFAULTS }
  }
}

/** Áp lên <html>: data-theme (light/dark/system), data-accent, data-font; color-scheme cho thanh cuộn/ô nhập. */
export function applyPreferences(prefs: Preferences = current) {
  const root = document.documentElement
  root.dataset.theme = prefs.theme
  if (prefs.accent === 'blue') delete root.dataset.accent
  else root.dataset.accent = prefs.accent
  if (prefs.font === 'normal') delete root.dataset.font
  else root.dataset.font = prefs.font
}

export function setPreferences(patch: Partial<Preferences>) {
  current = { ...current, ...patch }
  try {
    localStorage.setItem(KEY, JSON.stringify(current))
  } catch {
    /* chế độ riêng tư: chỉ áp cho phiên này */
  }
  applyPreferences()
  listeners.forEach((l) => l())
}

export function usePreferences(): Preferences {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener)
      return () => listeners.delete(listener)
    },
    () => current,
  )
}
