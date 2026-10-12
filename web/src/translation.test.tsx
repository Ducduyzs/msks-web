// @vitest-environment jsdom
// Dịch tài liệu trên API mock: tab Bản dịch trong trình xem tài liệu và trình xem bài giảng.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { RouterProvider, createMemoryRouter } from 'react-router-dom'
import { afterEach, beforeAll, describe, expect, it } from 'vitest'
import { routes } from './router'

beforeAll(() => {
  Element.prototype.scrollIntoView = () => {}
})
afterEach(cleanup)

function renderAt(path: string) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={createMemoryRouter(routes, { initialEntries: [path] })} />
    </QueryClientProvider>,
  )
}

const LONG = { timeout: 15_000 }

describe('Dịch tài liệu', () => {
  it('tài liệu: dịch sang tiếng Anh, xem song song, nhắc bản dịch chỉ để đọc', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_demo/sources')
    await screen.findByText(/RAPTOR: Recursive Abstractive Processing/)
    await user.click(screen.getByRole('button', { name: 'Xem nguồn S1' }))
    const drawer = await screen.findByRole('dialog', { name: 'Nguồn S1' })
    await user.click(within(drawer).getByRole('tab', { name: 'Bản dịch' }))
    expect(await within(drawer).findByText(/Nội dung nguồn sẽ được gửi tới nhà cung cấp LLM/)).toBeTruthy()
    expect((within(drawer).getByRole('combobox') as HTMLSelectElement).value).toBe('en')
    await user.click(within(drawer).getByRole('button', { name: /Dịch tài liệu/ }))
    expect(await within(drawer).findByText(/Bản dịch máy \(mock\), chỉ để đọc/, {}, LONG)).toBeTruthy()
    const translated = await within(drawer).findAllByText(/^〔en〕 /, {}, LONG)
    expect(translated.length).toBeGreaterThan(0)
    // Song song: đoạn gốc hiện cạnh bản dịch; "Chỉ bản dịch" ẩn bản gốc.
    expect(within(drawer).getAllByText(/Retrieval-augmented language models/).length).toBeGreaterThan(1)
    await user.click(within(drawer).getByRole('radio', { name: 'Chỉ bản dịch' }))
    expect(within(drawer).getAllByText(/Retrieval-augmented language models/)).toHaveLength(1)
  }, 30_000)

  it('bài giảng: bản dịch có mốc thời gian, bấm vào thì tua tới đúng slide', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_lecture/sources')
    await screen.findByText('Bài giảng: Agreement Ranking (mẫu, tiếng Việt)')
    await user.click(screen.getByRole('button', { name: 'Xem nguồn S1' }))
    const drawer = await screen.findByRole('dialog', { name: 'Bài giảng S1' })
    await user.click(await within(drawer).findByRole('tab', { name: 'Bản dịch' }))
    await user.click(await within(drawer).findByRole('button', { name: /Dịch tài liệu/ }))
    await within(drawer).findAllByText(/^〔en〕 Với 70 bài báo/, {}, LONG)
    await user.click(within(drawer).getAllByRole('button', { name: '0:22' }).at(-1)!)
    expect(within(drawer).getByText(/Khung 2\/4/)).toBeTruthy()
  }, 30_000)
})
