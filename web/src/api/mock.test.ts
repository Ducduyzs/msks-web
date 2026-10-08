import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { CodePointIndex } from '../lib/codepoints'
import { ApiError } from './client'
import { MockApi } from './mock'
import type { QaRequest, Run, RunEvent } from './types'

const WS = 'ws_demo'
const qa = (question: string, extra: Partial<QaRequest> = {}): QaRequest => ({
  question,
  source_ids: [],
  budget: 2048,
  mode: 'standard',
  ...extra,
})

let api: MockApi

beforeEach(() => {
  vi.useFakeTimers()
  api = new MockApi()
})
afterEach(() => vi.useRealTimers())

/** Gọi API rồi tua thời gian cho tới khi promise xong. */
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

async function finish(runId: string): Promise<{ run: Run; events: RunEvent[] }> {
  const events: RunEvent[] = []
  const handle = api.streamRun(runId, (e) => events.push(e))
  await vi.advanceTimersByTimeAsync(30_000)
  handle.close()
  return { run: await settle(api.getRun(runId)), events }
}

describe('MockApi — hỏi đáp', () => {
  it('trả lời với đủ trạng thái claim và offset code point khớp nguồn', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('RAPTOR xây dựng chỉ mục như thế nào?'), 'k1'))
    const { run, events } = await finish(run_id)
    expect(run.status).toBe('succeeded')
    expect(run.answer_status).toBe('answered')
    const status = Object.fromEntries(run.claims.map((c) => [c.id, c.status]))
    // S1↔S3 đã được đánh giá độc lập → CORROBORATED; S2↔S3 chưa xác định → chỉ SUPPORTED.
    expect(status).toMatchObject({ cl_method: 'corroborated', cl_data: 'supported', cl_packing: 'contested' })
    expect(run.claims.find((c) => c.id === 'cl_data')!.evidence.some((e) => e.role === 'corroborating')).toBe(true)
    expect(run.claims.find((c) => c.id === 'cl_cost')!.cross_check_status).toBe('incomplete')
    // S5 cùng nhóm với S3 nên không bao giờ là evidence đối chứng.
    expect(run.claims.flatMap((c) => c.evidence).some((e) => e.source_alias === 'S5')).toBe(false)

    for (const evidence of run.claims.flatMap((c) => c.evidence)) {
      const content = await settle(api.getSourceContent(evidence.source_id, evidence.parse_revision_id))
      const index = new CodePointIndex(content.text)
      expect(index.slice(evidence.quote_start, evidence.quote_end)).toBe(evidence.evidence_snapshot)
      expect(evidence.quote_start).toBeGreaterThanOrEqual(evidence.char_start)
      expect(evidence.quote_end).toBeLessThanOrEqual(evidence.char_end)
    }
    for (const claim of run.claims) expect(claim.evidence[0].context_block_id).toBeTruthy()

    const seqs = events.map((e) => e.seq)
    expect(seqs).toEqual([...seqs].sort((a, b) => a - b))
    expect(new Set(seqs).size).toBe(seqs.length)
    expect(events.at(-1)).toMatchObject({ type: 'done', status: 'succeeded' })
    expect(run.metrics!.accepted_count).toBe(run.claims.length)
    expect(run.metrics!.generated_count).toBe(run.claims.length + run.rejected_claims.length)
  })

  it('ngân sách nhỏ cho ít context hơn', async () => {
    const small = await settle(api.askQuestion(WS, qa('Kết quả chính?', { budget: 512 }), 'a'))
    const large = await settle(api.askQuestion(WS, qa('Kết quả chính?', { budget: 2048 }), 'b'))
    const [s, l] = [(await finish(small.run_id)).run, (await finish(large.run_id)).run]
    expect(s.context.length).toBeLessThan(l.context.length)
  })

  it('câu so sánh chỉ có một nhóm nguồn → không đủ bằng chứng', async () => {
    const request = qa('So sánh Agreement với RAPTOR', { source_ids: ['src_s3', 'src_s5'] })
    const { run_id } = await settle(api.askQuestion(WS, request, 'c'))
    const { run } = await finish(run_id)
    expect(run.answer_status).toBe('insufficient_evidence')
    expect(run.claims).toEqual([])
    expect(run.metrics!.citation_survival_rate).toBeNull()
  })

  it('lỗi định dạng mô hình được ghi nhận, không có claim', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('Câu hỏi [invalid]'), 'd'))
    const { run } = await finish(run_id)
    expect(run.answer_status).toBe('model_output_invalid')
    expect(run.metrics!.json_error_count).toBe(1)
    expect(run.claims).toEqual([])
  })

  it('verifier lỗi không mặc định pass', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('Câu hỏi [verifier-down]'), 'e'))
    const { run, events } = await finish(run_id)
    expect(run.answer_status).toBe('verification_unavailable')
    expect(run.claims).toEqual([])
    expect(events.some((e) => e.type === 'claim_verified')).toBe(false)
  })

  it('cùng Idempotency-Key trả về cùng run', async () => {
    const first = await settle(api.askQuestion(WS, qa('Câu hỏi lặp lại'), 'same'))
    const second = await settle(api.askQuestion(WS, qa('Câu hỏi lặp lại'), 'same'))
    expect(second.run_id).toBe(first.run_id)
  })

  it('hủy run: kết thúc CANCELLED, không publish sau khi hủy', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('RAPTOR xây dựng chỉ mục như thế nào?'), 'f'))
    const events: RunEvent[] = []
    const handle = api.streamRun(run_id, (e) => events.push(e))
    await vi.advanceTimersByTimeAsync(1500)
    await settle(api.cancelRun(run_id))
    await vi.advanceTimersByTimeAsync(30_000)
    handle.close()
    const run = await settle(api.getRun(run_id))
    expect(run.status).toBe('cancelled')
    expect(events.at(-1)).toMatchObject({ type: 'done', status: 'cancelled' })
    expect(events.filter((e) => e.type === 'claim_verified')).toHaveLength(0)
  })

  it('phạm vi rỗng bị từ chối với lỗi chuẩn', async () => {
    await expect(settle(api.askQuestion(WS, qa('Hỏi', { source_ids: ['src_s6'] }), 'g'))).rejects.toMatchObject({
      status: 422,
      code: 'empty_scope',
    })
  })
})

describe('MockApi — nguồn', () => {
  it('xóa nguồn: ẩn nội dung trong run cũ và trình xem trả 410', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('RAPTOR xây dựng chỉ mục như thế nào?'), 'h'))
    await finish(run_id)
    await settle(api.deleteSource('src_s1'))
    const run = await settle(api.getRun(run_id))
    const fromS1 = run.claims.flatMap((c) => c.evidence).filter((e) => e.source_id === 'src_s1')
    expect(fromS1.length).toBeGreaterThan(0)
    expect(fromS1.every((e) => e.source_deleted && e.evidence_snapshot === '')).toBe(true)
    const error = await settle(api.getSourceContent('src_s1', 'pr_s1_1')).catch((e: unknown) => e)
    expect(error).toBeInstanceOf(ApiError)
    expect((error as ApiError).status).toBe(410)
    await vi.advanceTimersByTimeAsync(3000)
    const page = await settle(api.listSources(WS))
    expect(page.items.some((s) => s.id === 'src_s1')).toBe(false)
  })

  it('vòng đời nạp đi tới READY và có revision', async () => {
    const file = new File(['# Tiêu đề\n\nĐoạn một.\n\nĐoạn hai.'], 'ghi-chu.md', { type: 'text/markdown' })
    const source = await settle(api.uploadSource(WS, file, 'up1'))
    expect(source.status).toBe('uploaded')
    await vi.advanceTimersByTimeAsync(4000)
    const listed = (await settle(api.listSources(WS))).items.find((s) => s.id === source.id)!
    expect(listed.status).toBe('ready')
    expect(listed.revision?.page_count).toBeNull()
    expect((await settle(api.getJob(source.job_id!))).state).toBe('succeeded')
  })

  it('PDF scan thất bại với mã lỗi', async () => {
    const file = new File(['x'], 'ban-scan.pdf', { type: 'application/pdf' })
    const source = await settle(api.uploadSource(WS, file, 'up2'))
    await vi.advanceTimersByTimeAsync(4000)
    const listed = (await settle(api.listSources(WS))).items.find((s) => s.id === source.id)!
    expect(listed.status).toBe('failed')
    expect(listed.error?.code).toBe('pdf_no_text_layer')
  })
})

describe('MockApi — tổng hợp và xuất', () => {
  it('mục thất bại làm run PARTIAL, mục tự thêm không có bằng chứng thì để trống', async () => {
    const outline = await settle(api.proposeOutline(WS, { topic: 'RAPTOR', source_ids: [] }))
    outline.push({ key: 'custom_a', title: 'Mục không có dữ liệu', questions: ['?'] })
    outline.push({ key: 'custom_b', title: 'Mục lỗi [fail]', questions: ['?'] })
    const { run_id } = await settle(api.startSynthesis(WS, { topic: 'RAPTOR', source_ids: [], outline, budget: 2048 }, 's1'))
    const { run } = await finish(run_id)
    expect(run.status).toBe('partial')
    const byKey = Object.fromEntries(run.sections!.map((s) => [s.key, s.status]))
    expect(byKey).toMatchObject({ method: 'complete', custom_a: 'insufficient_evidence', custom_b: 'failed' })
    const placed = run.sections!.flatMap((s) => s.paragraphs.flatMap((p) => p.claim_ids))
    expect(new Set(placed)).toEqual(new Set(run.claims.map((c) => c.id)))
  })

  it('xuất Markdown từ dữ liệu đã lưu; PDF chưa có trong MVP', async () => {
    const { run_id } = await settle(api.askQuestion(WS, qa('RAPTOR xây dựng chỉ mục như thế nào?'), 'x'))
    await finish(run_id)
    const markdown = await (await settle(api.exportRun(run_id, 'md'))).text()
    expect(markdown).toContain('RAPTOR dựng cây')
    await expect(settle(api.exportRun(run_id, 'pdf'))).rejects.toMatchObject({ status: 501 })
  })
})
