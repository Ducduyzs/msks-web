import clsx from 'clsx'
import { ChevronDown, FileX } from 'lucide-react'
import { useState } from 'react'
import type { ClaimResult, EvidenceRef } from '../api'
import { CROSS_CHECK, EVIDENCE_ROLE, pageLabel } from '../lib/labels'
import { ClaimStatusBadge } from './badges'
import { useViewer } from './viewerContext'

const ROLE_STYLE: Record<EvidenceRef['role'], string> = {
  primary: 'border-supported/40 bg-supported-soft text-supported',
  corroborating: 'border-corroborated/40 bg-corroborated-soft text-corroborated',
  contradicting: 'border-contested/40 bg-contested-soft text-contested',
}

export function CitationChip({ evidence }: { evidence: EvidenceRef }) {
  const { openEvidence } = useViewer()
  const page = pageLabel(evidence.page_start, evidence.page_end)
  if (evidence.source_deleted) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-xs text-muted" title="Nguồn đã bị xóa; nội dung đã ẩn">
        <FileX className="size-3" /> {evidence.source_alias} · đã xóa
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={() => openEvidence(evidence)}
      title={`${EVIDENCE_ROLE[evidence.role]} — bấm để xem đoạn gốc`}
      className={clsx('rounded border px-1.5 py-0.5 text-xs font-medium hover:brightness-95', ROLE_STYLE[evidence.role])}
    >
      {evidence.source_alias}
      {page && ` ${page}`}
    </button>
  )
}

/** Ghi chú đối chứng chéo: không suy ra đồng thuận khi chưa kiểm tra hoặc độc lập chưa xác định (mục 8.3). */
function crossCheckNote(claim: ClaimResult) {
  const others = claim.evidence.filter((e) => e.role === 'corroborating')
  if (claim.status === 'supported' && others.length) {
    return `Nguồn khác cũng hỗ trợ (${others.map((e) => e.source_alias).join(', ')}), nhưng chưa xác định là độc lập.`
  }
  if (claim.cross_check_status === 'completed' && claim.status === 'supported') {
    return 'Đã đối chứng: không tìm thấy nguồn khác trong phạm vi đã xét. Điều này không có nghĩa là không tồn tại.'
  }
  return CROSS_CHECK[claim.cross_check_status]
}

export function ClaimCard({ claim }: { claim: ClaimResult }) {
  const [open, setOpen] = useState(false)
  const { openEvidence } = useViewer()
  const sides = [
    { title: 'Hỗ trợ', items: claim.evidence.filter((e) => e.role !== 'contradicting') },
    { title: 'Mâu thuẫn', items: claim.evidence.filter((e) => e.role === 'contradicting') },
  ]
  return (
    <article
      className={clsx(
        'card p-4',
        claim.status === 'contested' && 'border-contested/50',
        claim.status === 'corroborated' && 'border-corroborated/40',
      )}
    >
      <div className="flex flex-wrap items-start gap-x-3 gap-y-2">
        <p className="min-w-0 flex-1 leading-relaxed">{claim.text}</p>
        <ClaimStatusBadge status={claim.status} />
      </div>
      <div className="mt-3 flex flex-wrap items-center gap-1.5">
        {claim.evidence.map((e) => (
          <CitationChip key={e.id} evidence={e} />
        ))}
        <button
          type="button"
          className="ml-auto inline-flex items-center gap-1 text-xs text-muted hover:text-fg"
          onClick={() => setOpen(!open)}
          aria-expanded={open}
        >
          Bằng chứng <ChevronDown className={clsx('size-3.5 transition-transform', open && 'rotate-180')} />
        </button>
      </div>
      <p className="mt-2 text-xs text-muted">{crossCheckNote(claim)}</p>
      {open && (
        <div className={clsx('mt-3 grid gap-3 border-t border-line pt-3', claim.status === 'contested' && 'sm:grid-cols-2')}>
          {sides
            .filter((side) => side.items.length)
            .map((side) => (
              <div key={side.title} className="space-y-2">
                {claim.status === 'contested' && <p className="label">{side.title}</p>}
                {side.items.map((e) => (
                  <button
                    key={e.id}
                    type="button"
                    disabled={e.source_deleted}
                    onClick={() => openEvidence(e)}
                    className="w-full rounded-md bg-surface-2 p-3 text-left text-sm enabled:hover:ring-1 enabled:hover:ring-line disabled:cursor-default"
                  >
                    <span className="label">
                      {EVIDENCE_ROLE[e.role]} · {e.source_alias}
                      {pageLabel(e.page_start, e.page_end) && ` · ${pageLabel(e.page_start, e.page_end)}`}
                    </span>
                    {e.source_deleted ? (
                      <span className="mt-1 block italic text-muted">Nguồn đã bị xóa — nội dung đã ẩn.</span>
                    ) : (
                      <q className="mt-1 block text-fg">{e.evidence_snapshot}</q>
                    )}
                  </button>
                ))}
              </div>
            ))}
        </div>
      )}
    </article>
  )
}
