// Luồng bài giảng trên API mock: cùng hợp đồng với backend (api_media.py, qa.py) để giao diện chạy được khi chưa có backend.
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ApiError } from './client'
import { MockApi } from './mock'
import { parseCaptions } from './mockLecture'
import type { Run } from './types'

let api: MockApi

beforeEach(() => {
  vi.useFakeTimers()
  api = new MockApi()
})
afterEach(() => vi.useRealTimers())

async function settle<T>(promise: Promise<T>, ms = 1000): Promise<T> {
  const result = promise.then(
    (value) => ({ value }),
    (error: unknown) => ({ error }),
  )
  await vi.advanceTimersByTimeAsync(ms)
  const outcome = await result
  if ('error' in outcome) throw outcome.error
  return outcome.value
}

async function finish(runId: string): Promise<Run> {
  const handle = api.streamRun(runId, () => {})
  await vi.advanceTimersByTimeAsync(30_000)
  handle.close()
  return settle(api.getRun(runId))
}

const SRT = '1\n00:00:01,000 --> 00:00:04,500\nHằng số k bằng 60.\n\n2\n00:00:05,000 --> 00:00:09,000\nVới 70 bài báo chỉ mất 32 giây.\n'

describe('MockApi — bài giảng', () => {
  it('bài giảng mẫu có timeline, khung hình và hỏi đáp với citation theo thời gian', async () => {
    const sources = (await settle(api.listSources('ws_lecture'))).items
    const lecture = sources.find((s) => s.kind === 'video')!
    expect(lecture.status).toBe('ready')
    const timeline = await settle(api.getTimeline(lecture.id, lecture.revision!.parse_revision_id))
    expect(timeline.segments).toHaveLength(8)
    expect(timeline.segments.some((s) => s.flags.includes('low_confidence'))).toBe(true)
    const frame = timeline.assets.find((a) => a.kind === 'frame')!
    expect((await settle(api.getMediaUrl(lecture.id, frame.id))).url).toMatch(/^data:image\/svg\+xml/)

    const { run_id } = await settle(api.askQuestion('ws_lecture', { question: 'Hằng số k bằng bao nhiêu?', source_ids: [], budget: 1024, mode: 'standard' }, 'q1'))
    const run = await finish(run_id)
    expect(run.profile).toBe('lecture_vi_v1')
    expect(run.claims.map((c) => c.evidence[0].modality).sort()).toEqual(['board', 'speech'])
    for (const claim of run.claims) {
      expect(claim.support_basis).toBe('automatic_transcript')
      const locator = claim.evidence[0].locator!
      expect(locator.items[0].start_ms).toBe(locator.start_ms)
    }
    expect(run.rejected_claims.map((r) => r.reason)).toContain('non_inferiority_overclaimed')
  })

  it('upload nhiều phần: complete khi thiếu phần → 409; đủ phần → nguồn video chạy pipeline tới ready', async () => {
    const size = 50 * 1024 * 1024 // > 45 MB → 2 phần
    const session = await settle(api.createMediaSession('ws_lecture', { filename: 'buoi-2.mp4', size_bytes: size, mime: 'video/mp4', language: 'vi', keep_original: false }, 'm1'))
    expect(session.part_count).toBe(2)
    await expect(settle(api.completeMediaSession(session.id))).rejects.toMatchObject({ code: 'upload_incomplete' })
    for (const part of session.parts) {
      await settle(api.uploadPart(part.upload_url!, new Blob([new Uint8Array(part.size)]), () => {}), 2000)
    }
    const source = await settle(api.completeMediaSession(session.id))
    expect(source.kind).toBe('video')
    expect(await settle(api.completeMediaSession(session.id))).toMatchObject({ id: source.id }) // idempotent
    await vi.advanceTimersByTimeAsync(6000)
    const ready = (await settle(api.listSources('ws_lecture'))).items.find((s) => s.id === source.id)!
    expect(ready.status).toBe('ready')
    expect(ready.capabilities?.original_playback_available).toBe('audio_and_frames')
  })

  it('YouTube cần phụ đề, chỉ có lời → ready_limited; trộn với tài liệu → 422 profile_index_missing', async () => {
    const caption = new File([SRT], 'phu-de.srt')
    await expect(settle(api.addYoutube('ws_demo', { url: 'https://example.com/x', language: 'vi', caption }))).rejects.toMatchObject({ code: 'invalid_youtube_url' })
    const source = await settle(api.addYoutube('ws_demo', { url: 'https://youtu.be/dQw4w9WgXcQ', language: 'vi', caption }))
    await vi.advanceTimersByTimeAsync(6000)
    const ready = (await settle(api.listSources('ws_demo'))).items.find((s) => s.id === source.id)!
    expect(ready.status).toBe('ready_limited')
    expect(ready.capabilities?.visual_available).toBe(false)

    const error = await settle(api.askQuestion('ws_demo', { question: 'Hằng số k?', source_ids: [], budget: 1024, mode: 'standard' }, 'q2')).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).code).toBe('profile_index_missing')
  })

  it('sửa bản chép → parse revision mới, đoạn đã sửa được tính là đã đối chiếu; bản cũ vẫn đọc được', async () => {
    const lecture = (await settle(api.listSources('ws_lecture'))).items.find((s) => s.kind === 'video')!
    const old = lecture.revision!.parse_revision_id
    const before = await settle(api.getTimeline(lecture.id, old))
    const flagged = before.segments.find((s) => s.flags.length)!
    const job = await settle(api.createTranscriptRevision(lecture.id, { base_revision_id: old, segments: [{ id: flagged.id, text: 'Hằng số k trong công thức được đặt bằng 60.' }], regions: [] }))
    expect(job.kind).toBe('reindex')
    await vi.advanceTimersByTimeAsync(2000)
    const updated = (await settle(api.listSources('ws_lecture'))).items.find((s) => s.id === lecture.id)!
    expect(updated.revision!.parse_revision_id).not.toBe(old)
    const after = await settle(api.getTimeline(lecture.id, updated.revision!.parse_revision_id))
    expect(after.segments.find((s) => s.id === flagged.id)).toMatchObject({ origin: 'manual_edit', reviewed: true, flags: [] })
    expect(after.parent_extraction_revision_id).toBe(before.extraction_revision_id)
    expect((await settle(api.getTimeline(lecture.id, old))).segments.find((s) => s.id === flagged.id)!.text).toBe(flagged.text)
  })

  it('glossary: bỏ trùng, giới hạn 40 thuật ngữ', async () => {
    expect((await settle(api.setAsrGlossary('ws_lecture', ['SBERT', ' sbert ', 'RAPTOR']))).terms).toEqual(['SBERT', 'RAPTOR'])
    expect((await settle(api.getWorkspace('ws_lecture'))).asr_glossary).toEqual(['SBERT', 'RAPTOR'])
    await expect(settle(api.setAsrGlossary('ws_lecture', Array.from({ length: 41 }, (_, i) => `t${i}`)))).rejects.toMatchObject({ code: 'invalid_glossary' })
  })
})

describe('parseCaptions', () => {
  it('đọc SRT và WebVTT, bỏ thẻ định dạng', () => {
    expect(parseCaptions(SRT)).toEqual([
      { start_ms: 1000, end_ms: 4500, text: 'Hằng số k bằng 60.' },
      { start_ms: 5000, end_ms: 9000, text: 'Với 70 bài báo chỉ mất 32 giây.' },
    ])
    expect(parseCaptions('WEBVTT\n\n00:01.000 --> 00:02.500 align:start\n<b>Xin chào</b>\n')).toEqual([{ start_ms: 1000, end_ms: 2500, text: 'Xin chào' }])
  })
})
