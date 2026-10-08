import { ChevronLeft } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { RunView } from '../components/run/RunView'

export function RunDetailPage() {
  const { runId = '' } = useParams()
  return (
    <div className="space-y-4">
      <Link to=".." relative="path" className="inline-flex items-center gap-1 text-sm text-muted hover:text-fg">
        <ChevronLeft className="size-4" /> Lịch sử
      </Link>
      <RunView key={runId} runId={runId} />
    </div>
  )
}
