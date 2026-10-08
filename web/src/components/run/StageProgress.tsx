import { Check } from 'lucide-react'
import type { RunStage } from '../../api'
import { Spinner } from '../ui'

export function StageProgress({ stages, running }: { stages: { stage: RunStage; label: string }[]; running: boolean }) {
  if (!stages.length) {
    return (
      <div className="flex items-center gap-2 text-sm text-muted" role="status">
        <Spinner /> Đang kết nối…
      </div>
    )
  }
  return (
    <ol className="space-y-1.5 text-sm" aria-live="polite">
      {stages.map((s, i) => {
        const current = running && i === stages.length - 1
        return (
          <li key={`${s.stage}-${i}`} className={current ? 'flex items-center gap-2 text-fg' : 'flex items-center gap-2 text-muted'}>
            {current ? <Spinner className="shrink-0 text-accent" /> : <Check className="size-4 shrink-0 text-corroborated" aria-hidden />}
            {s.label}
          </li>
        )
      })}
    </ol>
  )
}
