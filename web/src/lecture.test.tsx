// @vitest-environment jsdom
// Giao diện bài giảng trên API mock: citation theo thời gian → trình xem bài giảng; tải video theo phần.
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

describe('Bài giảng', () => {
  it('claim từ lời giảng có mốc thời gian, nhãn bản chép tự động và mở đúng đoạn trong trình xem', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_lecture/qa')
    await screen.findByText(/Tất cả nguồn sẵn sàng \(1\)/)
    await user.type(screen.getByLabelText('Câu hỏi'), 'Lập chỉ mục 70 bài báo mất bao lâu?')
    await user.click(screen.getByRole('button', { name: /Hỏi/ }))

    const claim = await screen.findByText('Lập chỉ mục 70 bài báo với Agreement Ranking mất 32 giây.', {}, LONG)
    const card = claim.closest('article')!
    expect(within(card).getByText('Được bản chép tự động hỗ trợ')).toBeTruthy()
    await user.click(within(card).getByRole('button', { name: /S1 0:22/ }))

    const drawer = await screen.findByRole('dialog', { name: 'Bài giảng S1' })
    expect(within(drawer).getByText(/Lời giảng · 0:22–0:30/)).toBeTruthy()
    const cited = await within(drawer).findByRole('button', { name: /thời gian lập chỉ mục chỉ mất 32 giây/ })
    expect(cited.closest('li')!.className).toContain('bg-highlight')
    // Trình xem dựng lại từ khung hình: tại 0:22 đang hiện slide "Chi phí lập chỉ mục".
    expect(within(drawer).getByText('70 bài: 32 giây so với 3097 giây')).toBeTruthy()
    expect(within(drawer).getByText(/Khung 2\/4/)).toBeTruthy()
  }, 40_000)

  it('sửa đoạn bị cảnh báo tạo phiên bản bản chép mới', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_lecture/sources')
    await screen.findByText('Bài giảng: Agreement Ranking (mẫu, tiếng Việt)')
    await user.click(screen.getByRole('button', { name: 'Xem nguồn S1' }))
    const drawer = await screen.findByRole('dialog', { name: 'Bài giảng S1' })
    expect(await within(drawer).findByText('Độ tin thấp')).toBeTruthy()

    await user.click(within(drawer).getByRole('button', { name: /Sửa bản chép/ }))
    const box = within(drawer).getByRole('textbox', { name: 'Sửa đoạn lúc 0:14' })
    await user.clear(box)
    await user.type(box, 'Hằng số k trong công thức được đặt bằng 60.')
    await user.click(within(drawer).getByRole('button', { name: /Lưu 1 chỗ sửa/ }))
    expect(await within(drawer).findByText('Đang lập chỉ mục lại')).toBeTruthy()
  }, 30_000)

  it('tải video theo phần, hiện tiến độ rồi tạo nguồn mới', async () => {
    const user = userEvent.setup()
    renderAt('/w/ws_lecture/sources')
    await screen.findByText('Thêm bài giảng')
    const video = new File([new Uint8Array(1024)], 'buoi-3.mp4', { type: 'video/mp4' })
    await user.upload(screen.getByLabelText('Video bài giảng'), video)
    await user.click(screen.getByRole('button', { name: /Tải lên và xử lý/ }))
    expect(await screen.findByText('Đã tạo nguồn S2', {}, LONG)).toBeTruthy()
    await waitFor(() => expect(screen.getByText('buoi-3')).toBeTruthy(), LONG)
  }, 30_000)
})
