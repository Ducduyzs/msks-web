import { createContext, useContext } from 'react'
import type { EvidenceRef, Source } from '../api'

export interface ViewerApi {
  openSource: (source: Source) => void
  openEvidence: (evidence: EvidenceRef) => void
}

export const ViewerContext = createContext<ViewerApi | null>(null)

/** Mở trình xem nguồn từ bất kỳ đâu trong workspace. */
export function useViewer() {
  const value = useContext(ViewerContext)
  if (!value) throw new Error('useViewer cần nằm trong ViewerProvider')
  return value
}
