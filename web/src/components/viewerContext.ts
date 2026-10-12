import { createContext, useContext } from 'react'
import type { EvidenceRef, Source } from '../api'
import { t } from '../i18n'

export interface ViewerApi {
  openSource: (source: Source) => void
  openEvidence: (evidence: EvidenceRef) => void
}

export const ViewerContext = createContext<ViewerApi | null>(null)

/** Mở trình xem nguồn từ bất kỳ đâu trong workspace. */
export function useViewer() {
  const value = useContext(ViewerContext)
  if (!value) throw new Error(t('useViewer cần nằm trong ViewerProvider'))
  return value
}
