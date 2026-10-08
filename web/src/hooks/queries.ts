import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { api, type Source } from '../api'
import { isIngesting } from '../lib/labels'

export function useReadiness() {
  return useQuery({ queryKey: ['ready'], queryFn: () => api.getReadiness(), refetchInterval: 30_000, retry: false })
}

export function useWorkspace(workspaceId: string) {
  return useQuery({ queryKey: ['workspace', workspaceId], queryFn: () => api.getWorkspace(workspaceId) })
}

/** Toàn bộ nguồn (gộp các trang cursor); tự làm mới khi còn nguồn đang nạp hoặc đang xóa. */
export function useSources(workspaceId: string) {
  return useQuery({
    queryKey: ['sources', workspaceId],
    queryFn: async () => {
      const all: Source[] = []
      let cursor: string | undefined
      do {
        const page = await api.listSources(workspaceId, cursor)
        all.push(...page.items)
        cursor = page.next_cursor ?? undefined
      } while (cursor)
      return all
    },
    refetchInterval: (query) =>
      query.state.data?.some((s) => isIngesting(s.status) || s.status === 'deleting') ? 1500 : false,
  })
}

export function useRuns(workspaceId: string) {
  return useInfiniteQuery({
    queryKey: ['runs', workspaceId],
    queryFn: ({ pageParam }) => api.listRuns(workspaceId, pageParam),
    initialPageParam: undefined as string | undefined,
    getNextPageParam: (last) => last.next_cursor ?? undefined,
    refetchInterval: (query) =>
      query.state.data?.pages.some((p) => p.items.some((r) => r.status === 'queued' || r.status === 'running')) ? 2000 : false,
  })
}
