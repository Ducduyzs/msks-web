import { useQueryClient } from '@tanstack/react-query'
import { useEffect, useReducer } from 'react'
import { api } from '../api'
import type { ApiErrorBody, ClaimResult, ReportSection, RunEvent, RunStage, RunStatus } from '../api'

export interface StreamState {
  lastSeq: number
  stages: { stage: RunStage; label: string }[]
  /** Claim đã kiểm chứng, theo thứ tự xuất hiện; cập nhật tại chỗ khi đối chứng chéo xong. */
  claims: ClaimResult[]
  sections: ReportSection[]
  done?: RunStatus
  error?: ApiErrorBody
  connection: 'connecting' | 'open' | 'reconnecting'
}

type Action = RunEvent | { type: 'reset' } | { type: 'connection'; state: 'open' | 'reconnecting' }

export const initialStream: StreamState = { lastSeq: 0, stages: [], claims: [], sections: [], connection: 'connecting' }

export function streamReducer(state: StreamState, action: Action): StreamState {
  if (action.type === 'reset') return initialStream
  if (action.type === 'connection') return { ...state, connection: action.state }
  // Dedup theo seq: khi kết nối lại, server phát lại các event đã gửi (mục 4.3).
  if (action.seq <= state.lastSeq) return state
  const next = { ...state, lastSeq: action.seq }
  switch (action.type) {
    case 'stage':
      return { ...next, stages: [...state.stages, { stage: action.stage, label: action.label }] }
    case 'claim_verified': {
      const index = state.claims.findIndex((c) => c.id === action.claim.id)
      const claims = index < 0 ? [...state.claims, action.claim] : state.claims.map((c, i) => (i === index ? action.claim : c))
      return { ...next, claims }
    }
    case 'section':
      return { ...next, sections: [...state.sections.filter((s) => s.key !== action.section.key), action.section] }
    case 'done':
      return { ...next, done: action.status }
    case 'error':
      return { ...next, error: action.error }
  }
}

/** Nhận sự kiện SSE của một run đang chạy; khi xong thì làm mới dữ liệu run đã persist. */
export function useRunStream(runId: string | undefined) {
  const [state, dispatch] = useReducer(streamReducer, initialStream)
  const queryClient = useQueryClient()

  useEffect(() => {
    dispatch({ type: 'reset' })
    if (!runId) return
    const handle = api.streamRun(
      runId,
      (event) => {
        dispatch(event)
        if (event.type === 'done') {
          queryClient.invalidateQueries({ queryKey: ['run', runId] })
          queryClient.invalidateQueries({ queryKey: ['runs'] })
          queryClient.invalidateQueries({ queryKey: ['workspace'] })
        }
      },
      (connection) => dispatch({ type: 'connection', state: connection }),
    )
    return () => handle.close()
  }, [runId, queryClient])

  return state
}
