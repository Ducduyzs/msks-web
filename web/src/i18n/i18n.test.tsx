// @vitest-environment jsdom
// Đa ngôn ngữ giao diện + tùy chọn giao diện (sáng/tối, màu nhấn, cỡ chữ).
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { LanguageRoot } from '../components/PreferencesMenu'
import { applyPreferences, setPreferences } from '../lib/preferences'
import { routes } from '../router'
import { LANGUAGES, currentLocale, msg, setLanguage, t, translated } from './index'

const catalog = (import.meta.glob<{ messages: string[] }>('./catalog.json', { eager: true, import: 'default' })['./catalog.json']).messages
const locales = import.meta.glob<Record<string, string>>('./locales/*.json', { eager: true, import: 'default' })
const placeholders = (s: string) => (s.match(/\{\w+\}/g) ?? []).sort()

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
})
afterEach(async () => {
  cleanup()
  await setLanguage('vi')
  setPreferences({ theme: 'system', accent: 'blue', font: 'normal' })
})

describe('t()', () => {
  it('tiếng Việt là ngôn ngữ nguồn; biến được thay; câu thiếu bản dịch hiện tiếng Việt', async () => {
    expect(t('Đã tạo nguồn {0}', { 0: 'S2' })).toBe('Đã tạo nguồn S2')
    await setLanguage('en')
    expect(t('Đã tạo nguồn {0}', { 0: 'S2' })).toBe('Source S2 created')
    expect(t('Câu chưa có trong từ điển')).toBe('Câu chưa có trong từ điển')
    expect(currentLocale()).toBe('en-US')
    expect(document.documentElement.lang).toBe('en')
  })

  it('bảng nhãn khai báo ở mức module được dịch lúc đọc', async () => {
    const table = translated({ a: msg('Đăng xuất'), nested: { label: msg('Sẵn sàng') }, none: null })
    expect(table.a).toBe('Đăng xuất')
    await setLanguage('en')
    expect(table.a).toBe('Sign out')
    expect(table.nested.label).toBe('Ready')
    expect(table.none).toBeNull()
  })
})

describe('từ điển', () => {
  for (const { code } of LANGUAGES.filter((l) => l.code !== 'vi')) {
    it(`${code}: đủ mọi câu của danh mục, giữ nguyên biến {…}`, () => {
      const dict = locales[`./locales/${code}.json`]
      const missing = catalog.filter((m) => !(m in dict))
      expect(missing).toEqual([])
      const broken = catalog.filter((m) => JSON.stringify(placeholders(m)) !== JSON.stringify(placeholders(dict[m])))
      expect(broken).toEqual([])
    })
  }
})

describe('tùy chọn giao diện', () => {
  it('đặt data-theme / data-accent / data-font trên <html> và lưu lại', () => {
    setPreferences({ theme: 'dark', accent: 'teal', font: 'large' })
    const root = document.documentElement
    expect([root.dataset.theme, root.dataset.accent, root.dataset.font]).toEqual(['dark', 'teal', 'large'])
    expect(JSON.parse(localStorage.getItem('msks.preferences')!)).toMatchObject({ theme: 'dark', accent: 'teal' })
    setPreferences({ accent: 'blue', font: 'normal' })
    expect(root.dataset.accent).toBeUndefined()
    applyPreferences()
    expect(root.dataset.theme).toBe('dark')
  })

  it('đổi sang tiếng Anh và chế độ tối từ menu Giao diện', async () => {
    const user = userEvent.setup()
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
    render(
      <QueryClientProvider client={client}>
        <LanguageRoot>
          <RouterProvider router={createMemoryRouter(routes, { initialEntries: ['/w/ws_demo/sources'] })} />
        </LanguageRoot>
      </QueryClientProvider>,
    )
    await screen.findByText('Nguồn trong workspace')
    await user.click(screen.getByRole('button', { name: 'Giao diện và ngôn ngữ' }))
    const dialog = screen.getByRole('dialog', { name: 'Giao diện và ngôn ngữ' })
    await user.click(within(dialog).getByRole('radio', { name: /Tối/ }))
    expect(document.documentElement.dataset.theme).toBe('dark')
    await user.selectOptions(within(dialog).getByRole('combobox'), 'en')
    await waitFor(() => expect(screen.getByText('Sources in this workspace')).toBeTruthy())
    expect(screen.getByRole('link', { name: 'Q&A' })).toBeTruthy()
  }, 20_000)
})
