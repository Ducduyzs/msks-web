// @vitest-environment jsdom
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router-dom'
import { afterEach, expect, it, vi } from 'vitest'
import { api, type Run } from '../../api'
import { RunView } from './RunView'

afterEach(() => { cleanup(); vi.restoreAllMocks() })

const completed: Run = {
  run_id: 'r', workspace_id: 'w', mode: 'qa', profile: 'product', query: 'Original question',
  budget: { context_tokens: 512 }, source_ids: [], status: 'succeeded',
  answer_status: 'insufficient_evidence', cancel_requested: false,
  claims: [], rejected_claims: [], context: [], created_at: '2026-10-08T00:00:00Z',
  provenance: {
    profile: 'product', config_hash: 'c', code_hash: 'h', edahr_commit: null,
    verifier_revision: 'v', calibration_artifact: null, grouping_version: 1, models: {},
    source_snapshot: [
      { source_id: 'old-source', alias: 'S1', document_revision_id: 'd1', parse_revision_id: 'p1' },
    ],
  },
}

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  return render(<QueryClientProvider client={client}><MemoryRouter><RunView runId="r" /></MemoryRouter></QueryClientProvider>)
}

it('rerun uses the original source list and reuses its key after a network failure', async () => {
  vi.spyOn(api, 'getRun').mockResolvedValue(completed)
  const ask = vi.spyOn(api, 'askQuestion').mockRejectedValue(new Error('Connection lost'))
  show()
  const user = userEvent.setup()
  const button = await screen.findByRole('button', { name: 'Chạy lại câu hỏi' })
  await user.click(button)
  await screen.findByText('Connection lost')
  await user.click(button)
  await waitFor(() => expect(ask).toHaveBeenCalledTimes(2))
  expect(ask.mock.calls[0][1].source_ids).toEqual(['old-source'])
  expect(ask.mock.calls[0][2]).toBe(ask.mock.calls[1][2])
})

it('recovers the final result when SSE never delivers done', async () => {
  const read = vi.spyOn(api, 'getRun').mockResolvedValueOnce({ ...completed, status: 'running' }).mockResolvedValue(completed)
  const close = vi.fn()
  vi.spyOn(api, 'streamRun').mockReturnValue({ close })
  show()
  await screen.findByRole('button', { name: 'Hủy lượt chạy' })
  await screen.findByRole('button', { name: 'Chạy lại câu hỏi' }, { timeout: 7500 })
  expect(read.mock.calls.length).toBeGreaterThanOrEqual(2)
  await waitFor(() => expect(close).toHaveBeenCalled())
}, 10000)
