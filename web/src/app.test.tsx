// @vitest-environment jsdom
// Kiểm thử giao diện end-to-end trên API mock: hỏi → stream claim → mở trích dẫn.
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
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
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  )
  return router
}

const LONG = { timeout: 20_000 }

describe('Hỏi đáp', () => {
  it('stream claim đã kiểm chứng, cập nhật đối chứng chéo và mở đúng đoạn gốc', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_demo/qa')
    await screen.findByText('Demo: Agreement Ranking và RAPTOR')
    await screen.findByText(/Tất cả nguồn sẵn sàng \(5\)/)

    await user.type(screen.getByLabelText('Câu hỏi'), 'RAPTOR xây dựng chỉ mục như thế nào?')
    await user.click(screen.getByRole('radio', { name: '512' }))
    await user.click(screen.getByRole('button', { name: /Hỏi/ }))

    const claim = await screen.findByText(/RAPTOR dựng cây bằng cách đệ quy/, {}, LONG)
    const card = claim.closest('article')!
    // Sau bước đối chứng chéo: S1 ↔ S3 đã được đánh giá độc lập.
    await within(card).findByText('Có đối chứng độc lập', {}, LONG)
    await screen.findByText('Chỉ số kiểm chứng', {}, LONG)
    expect(screen.getByText('Kết quả thử nghiệm')).toBeTruthy()
    // Claim của S2 có nguồn khác hỗ trợ nhưng độc lập chưa xác định → không nâng nhãn.
    const dataCard = screen.getAllByText(/Các câu hỏi trong PeerQA/)[0].closest('article')!
    expect(within(dataCard).getByText('Được bằng chứng hỗ trợ')).toBeTruthy()
    expect(within(dataCard).getByText(/chưa xác định là độc lập/)).toBeTruthy()

    await user.click(within(card).getByRole('button', { name: 'S1 tr. 2' }))
    const dialog = await screen.findByRole('dialog', { name: 'Nguồn S1' })
    await waitFor(() => {
      const marks = [...dialog.querySelectorAll('mark')].map((m) => m.textContent)
      expect(marks).toContain(
        'RAPTOR recursively embeds, clusters, and summarizes chunks of text, constructing a tree with differing levels of summarization from the bottom up.',
      )
    })
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
  }, 40_000)

  it('câu so sánh với một nhóm nguồn trả về "Không đủ bằng chứng"', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_demo/qa')
    await screen.findByText(/Tất cả nguồn sẵn sàng \(5\)/)
    await user.click(screen.getByRole('button', { name: /^S3/ }))
    await user.type(screen.getByLabelText('Câu hỏi'), 'So sánh Agreement với RAPTOR')
    await user.click(screen.getByRole('button', { name: /Hỏi/ }))
    const notices = await screen.findAllByText('Không đủ bằng chứng', {}, LONG)
    expect(notices.length).toBeGreaterThan(0)
    expect(screen.getByText(/ít nhất hai nhóm nguồn/)).toBeTruthy()
  }, 40_000)
})

describe('Nguồn', () => {
  it('hiện nguồn lỗi, nhóm trùng và không cho xem nguồn chưa sẵn sàng', async () => {
    renderAt('/w/ws_demo/sources')
    await screen.findByText('Bản scan hội thảo 2019')
    expect(screen.getByText(/pdf_no_text_layer/)).toBeTruthy()
    expect(screen.getAllByText(/cùng nhóm với/).length).toBeGreaterThanOrEqual(2)
    expect((screen.getByRole('button', { name: 'Xem nguồn S6' }) as HTMLButtonElement).disabled).toBe(true)
  })
})
