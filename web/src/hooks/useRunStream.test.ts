import { describe, expect, it } from 'vitest'
import type { ClaimResult, RunEvent } from '../api'
import { initialStream, streamReducer } from './useRunStream'

const claim = (id: string, status: ClaimResult['status'] = 'supported'): ClaimResult => ({
  id,
  text: id,
  status,
  cross_check_status: 'not_run',
  verification_method: 'nli',
  generator_confidence: 0.9,
  evidence: [],
})

function run(events: RunEvent[]) {
  return events.reduce(streamReducer, initialStream)
}

describe('streamReducer', () => {
  it('bỏ qua event đã nhận khi server phát lại sau kết nối lại', () => {
    const events: RunEvent[] = [
      { seq: 1, type: 'stage', stage: 'queued', label: 'q' },
      { seq: 2, type: 'claim_verified', claim: claim('a') },
    ]
    const state = run([...events, ...events, { seq: 3, type: 'done', status: 'succeeded' }])
    expect(state.stages).toHaveLength(1)
    expect(state.claims).toHaveLength(1)
    expect(state.done).toBe('succeeded')
    expect(state.lastSeq).toBe(3)
  })

  it('cập nhật claim tại chỗ khi đối chứng chéo xong, giữ thứ tự', () => {
    const state = run([
      { seq: 1, type: 'claim_verified', claim: claim('a') },
      { seq: 2, type: 'claim_verified', claim: claim('b') },
      { seq: 3, type: 'claim_verified', claim: claim('a', 'contested') },
    ])
    expect(state.claims.map((c) => [c.id, c.status])).toEqual([
      ['a', 'contested'],
      ['b', 'supported'],
    ])
  })

  it('trạng thái kết nối không ảnh hưởng seq', () => {
    const state = [{ type: 'connection', state: 'reconnecting' } as const].reduce(streamReducer, initialStream)
    expect(state.connection).toBe('reconnecting')
    expect(state.lastSeq).toBe(0)
  })
})
