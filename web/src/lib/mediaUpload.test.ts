// @vitest-environment jsdom
// Điều phối tải video theo phần: tiếp tục phiên cũ, URL ký hết hạn, tạm dừng, phần sai kích thước.
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError, type Api, type MediaUploadSession, type Source } from '../api'
import { formatMs, resumeKey, uploadVideo } from './mediaUpload'

const WS = 'ws_test'
const PART = 4

function session(uploaded: number[] = [], extra: Partial<MediaUploadSession> = {}): MediaUploadSession {
  const sizes = [PART, PART, 2]
  return {
    id: 'ups_1',
    workspace_id: WS,
    status: 'open',
    filename: 'bai-giang.mp4',
    mime: 'video/mp4',
    expected_bytes: 10,
    part_bytes: PART,
    part_count: 3,
    language: 'vi',
    keep_original: false,
    caption: null,
    parts: sizes.map((size, index) =>
      uploaded.includes(index) ? { index, size, uploaded: true } : { index, size, uploaded: false, upload_url: `https://storage/ups_1/${index}?v=1`, method: 'PUT' },
    ),
    ...extra,
  }
}

function fakeApi(overrides: Partial<Api> = {}) {
  const uploaded: { url: string; bytes: number }[] = []
  const api = {
    createMediaSession: vi.fn(async () => session()),
    getMediaSession: vi.fn(async () => session()),
    uploadPart: vi.fn(async (url: string, blob: Blob, onProgress: (n: number) => void) => {
      onProgress(blob.size)
      uploaded.push({ url, bytes: blob.size })
    }),
    attachCaption: vi.fn(async () => session([], { caption: { filename: 'phu-de.srt' } })),
    completeMediaSession: vi.fn(async () => ({ id: 'src_1', alias: 'S1' }) as Source),
    cancelMediaSession: vi.fn(async () => {}),
    ...overrides,
  } as unknown as Api
  return { api, uploaded }
}

const video = () => new File([new Uint8Array(10)], 'bai-giang.mp4', { type: 'video/mp4', lastModified: 1 })

beforeEach(() => localStorage.clear())

describe('uploadVideo', () => {
  it('tải đủ các phần đúng offset, báo tiến độ và quên phiên sau khi complete', async () => {
    const { api, uploaded } = fakeApi()
    const progress: number[] = []
    const source = await uploadVideo(api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false, onProgress: (p) => progress.push(p.sentBytes) })
    expect(source.alias).toBe('S1')
    expect(uploaded.map((u) => u.bytes)).toEqual([4, 4, 2])
    expect(progress.at(-1)).toBe(10)
    expect(localStorage.getItem(resumeKey(WS, video()))).toBeNull()
  })

  it('chọn lại đúng file thì tiếp tục phiên cũ, chỉ tải phần còn thiếu', async () => {
    localStorage.setItem(resumeKey(WS, video()), 'ups_1')
    const { api, uploaded } = fakeApi({ getMediaSession: vi.fn(async () => session([0, 1])) })
    let resumed = false
    await uploadVideo(api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false, onProgress: (p) => (resumed = p.resumed) })
    expect(api.createMediaSession).not.toHaveBeenCalled()
    expect(uploaded.map((u) => u.url)).toEqual(['https://storage/ups_1/2?v=1'])
    expect(resumed).toBe(true)
  })

  it('URL ký hết hạn (403) → lấy URL mới rồi tải lại phần đó', async () => {
    let calls = 0
    const { api, uploaded } = fakeApi({
      uploadPart: vi.fn(async (url: string, blob: Blob) => {
        if (url.endsWith('/1?v=1') && calls++ === 0) {
          throw new ApiError(403, { code: 'part_upload_403', message: 'hết hạn', retryable: true })
        }
        uploaded.push({ url, bytes: blob.size })
      }),
      getMediaSession: vi.fn(async () => {
        const fresh = session([0])
        fresh.parts[1].upload_url = 'https://storage/ups_1/1?v=2'
        return fresh
      }),
    })
    await uploadVideo(api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false })
    expect(uploaded.map((u) => u.url)).toContain('https://storage/ups_1/1?v=2')
  })

  it('tạm dừng giữ phiên để tiếp tục; lỗi không thử lại được thì ném ra ngay', async () => {
    const controller = new AbortController()
    const { api } = fakeApi({
      uploadPart: vi.fn(async () => {
        controller.abort()
        throw new DOMException('Đã dừng tải lên.', 'AbortError')
      }),
    })
    await expect(uploadVideo(api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false, signal: controller.signal })).rejects.toMatchObject({
      name: 'AbortError',
    })
    expect(localStorage.getItem(resumeKey(WS, video()))).toBe('ups_1')

    const bad = fakeApi({ createMediaSession: vi.fn(async () => ({ ...session(), parts: [{ index: 0, size: 4, uploaded: false, error: 'size_mismatch' as const }] })) })
    localStorage.clear()
    await expect(uploadVideo(bad.api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false })).rejects.toMatchObject({ code: 'part_size_mismatch' })
  })

  it('kèm phụ đề một lần; từ chối định dạng không phải video', async () => {
    const { api } = fakeApi()
    const caption = new File(['1\n00:00:00,000 --> 00:00:01,000\nxin chào'], 'phu-de.srt')
    await uploadVideo(api, { workspaceId: WS, file: video(), language: 'vi', keepOriginal: false, caption })
    expect(api.attachCaption).toHaveBeenCalledTimes(1)
    await expect(uploadVideo(api, { workspaceId: WS, file: new File(['x'], 'slides.pdf'), language: 'vi', keepOriginal: false })).rejects.toMatchObject({
      code: 'unsupported_media_type',
    })
  })
})

describe('formatMs', () => {
  it('định dạng mm:ss và h:mm:ss', () => {
    expect(formatMs(22_440)).toBe('0:22')
    expect(formatMs(3_725_000)).toBe('1:02:05')
    expect(formatMs(null)).toBe('—')
  })
})
