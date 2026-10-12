import clsx from 'clsx'
import { Check, Languages, Monitor, Moon, Palette, Sun } from 'lucide-react'
import { Fragment, useEffect, useRef, useState, type ReactNode } from 'react'
import { LANGUAGES, SOURCE_LANGUAGE, msg, setLanguage, t, translated, useLanguage } from '../i18n'
import { ACCENTS, setPreferences, usePreferences, type Accent, type FontSize, type ThemeMode } from '../lib/preferences'

const ACCENT_NAME: Record<Accent, string> = translated({
  blue: msg('Xanh dương'),
  indigo: msg('Chàm'),
  violet: msg('Tím'),
  teal: msg('Xanh ngọc'),
  rose: msg('Hồng'),
  slate: msg('Xám'),
})

/** Cả ứng dụng render lại khi đổi ngôn ngữ: chữ, bảng nhãn và định dạng ngày đều theo ngôn ngữ mới. */
export function LanguageRoot({ children }: { children: ReactNode }) {
  const language = useLanguage()
  return <Fragment key={language}>{children}</Fragment>
}

/** Nút "Giao diện": ngôn ngữ, chế độ sáng/tối, màu nhấn, cỡ chữ. Lưu trong trình duyệt này. */
export function PreferencesMenu() {
  const [open, setOpen] = useState(false)
  const panel = useRef<HTMLDivElement>(null)
  const button = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        setOpen(false)
        button.current?.focus()
      }
    }
    const onClick = (e: MouseEvent) => {
      if (!panel.current?.contains(e.target as Node) && !button.current?.contains(e.target as Node)) setOpen(false)
    }
    window.addEventListener('keydown', onKey)
    window.addEventListener('mousedown', onClick)
    return () => {
      window.removeEventListener('keydown', onKey)
      window.removeEventListener('mousedown', onClick)
    }
  }, [open])

  return (
    <div className="relative">
      <button
        ref={button}
        className="btn btn-ghost px-2 py-1"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
        aria-haspopup="dialog"
        aria-label={t('Giao diện và ngôn ngữ')}
        title={t('Giao diện và ngôn ngữ')}
      >
        <Palette className="size-4" />
      </button>
      {open && (
        <div ref={panel} role="dialog" aria-label={t('Giao diện và ngôn ngữ')} className="card absolute right-0 z-50 mt-2 w-72 space-y-4 p-4 shadow-lg">
          <PreferencesPanel />
        </div>
      )}
    </div>
  )
}

export function PreferencesPanel() {
  const prefs = usePreferences()
  const language = useLanguage()
  const modes: [ThemeMode, string, typeof Sun][] = [
    ['light', t('Sáng'), Sun],
    ['dark', t('Tối'), Moon],
    ['system', t('Theo hệ thống'), Monitor],
  ]
  const fonts: [FontSize, string][] = [
    ['small', t('Nhỏ')],
    ['normal', t('Vừa')],
    ['large', t('Lớn')],
  ]
  return (
    <>
      <label className="block space-y-1 text-sm">
        <span className="label inline-flex items-center gap-1">
          <Languages className="size-3.5" aria-hidden /> {t('Ngôn ngữ giao diện')}
        </span>
        <select className="input" value={language} onChange={(e) => void setLanguage(e.target.value)}>
          {LANGUAGES.map((l) => (
            <option key={l.code} value={l.code}>
              {l.name}
            </option>
          ))}
        </select>
        {language !== SOURCE_LANGUAGE && language !== 'en' && (
          <span className="block text-xs text-muted">{t('Bản dịch giao diện do máy tạo, có thể chưa chuẩn.')}</span>
        )}
      </label>

      <fieldset className="space-y-1">
        <legend className="label">{t('Chế độ')}</legend>
        <div className="grid grid-cols-3 gap-1 rounded-md border border-line p-0.5" role="radiogroup">
          {modes.map(([id, label, Icon]) => (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={prefs.theme === id}
              onClick={() => setPreferences({ theme: id })}
              className={clsx('flex flex-col items-center gap-0.5 rounded px-1 py-1.5 text-xs', prefs.theme === id ? 'bg-accent text-accent-fg' : 'text-muted hover:bg-surface-2')}
            >
              <Icon className="size-4" aria-hidden />
              {label}
            </button>
          ))}
        </div>
      </fieldset>

      <fieldset className="space-y-1">
        <legend className="label">{t('Màu nhấn')}</legend>
        <div className="flex flex-wrap gap-2" role="radiogroup">
          {ACCENTS.map((a) => (
            <button
              key={a.id}
              type="button"
              role="radio"
              aria-checked={prefs.accent === a.id}
              aria-label={ACCENT_NAME[a.id]}
              title={ACCENT_NAME[a.id]}
              onClick={() => setPreferences({ accent: a.id })}
              className={clsx('flex size-8 items-center justify-center rounded-full ring-offset-2 ring-offset-surface', prefs.accent === a.id && 'ring-2 ring-fg')}
              // Màu mẫu cố định (không phụ thuộc chế độ); React đặt style qua CSSOM nên không vướng CSP.
              style={{ backgroundColor: a.swatch }}
            >
              {prefs.accent === a.id && <Check className="size-4 text-white" aria-hidden />}
            </button>
          ))}
        </div>
      </fieldset>

      <fieldset className="space-y-1">
        <legend className="label">{t('Cỡ chữ')}</legend>
        <div className="grid grid-cols-3 gap-1 rounded-md border border-line p-0.5" role="radiogroup">
          {fonts.map(([id, label]) => (
            <button
              key={id}
              type="button"
              role="radio"
              aria-checked={prefs.font === id}
              onClick={() => setPreferences({ font: id })}
              className={clsx('rounded px-1 py-1 text-xs', prefs.font === id ? 'bg-accent text-accent-fg' : 'text-muted hover:bg-surface-2')}
            >
              {label}
            </button>
          ))}
        </div>
      </fieldset>
    </>
  )
}
