import clsx from 'clsx'
import { ChevronDown, FileX, Mic, Presentation, Subtitles } from 'lucide-react'
import { useState } from 'react'
import type { ClaimResult, EvidenceRef } from '../api'
import { CROSS_CHECK, EVIDENCE_ROLE, MODALITY, SUPPORT_BASIS, pageLabel } from '../lib/labels'
import { formatMs } from '../lib/mediaUpload'
import { ClaimStatusBadge } from './badges'
import { Pill } from './ui'
import { useViewer } from './viewerContext'
import { t } from '../i18n'

const MODALITY_ICON = { speech: Mic, caption: Subtitles, board: Presentation } as const

/** Vị trí đọc được của evidence: trang với tài liệu, mốc thời gian với bài giảng. */
function evidenceWhere(evidence: EvidenceRef) {
  const start = evidence.locator?.start_ms
  if (start !== undefined && start !== null) return formatMs(start)
  return pageLabel(evidence.page_start, evidence.page_end)
}

const ROLE_STYLE: Record<EvidenceRef['role'], string> = {
  primary: 'border-supported/40 bg-supported-soft text-supported',
  corroborating: 'border-corroborated/40 bg-corroborated-soft text-corroborated',
  contradicting: 'border-contested/40 bg-contested-soft text-contested',
}

export function CitationChip({ evidence }: { evidence: EvidenceRef }) {
  const { openEvidence } = useViewer()
  const page = evidenceWhere(evidence)
  const Icon = evidence.modality && evidence.modality !== 'text' ? MODALITY_ICON[evidence.modality] : null
  if (evidence.source_deleted) {
    return (
      <span className="inline-flex items-center gap-1 rounded border border-line px-1.5 py-0.5 text-xs text-muted" title={t('Nguồn đã bị xóa; nội dung đã ẩn')}>
        <FileX className="size-3" /> {evidence.source_alias} {t('· đã xóa')}
      </span>
    )
  }
  return (
    <button
      type="button"
      onClick={() => openEvidence(evidence)}
      title={t('{0}{1} — bấm để xem đoạn gốc', { 0: EVIDENCE_ROLE[evidence.role], 1: evidence.modality && evidence.modality !== 'text' ? ` · ${MODALITY[evidence.modality]}` : '' })}
      className={clsx('inline-flex items-center gap-1 rounded border px-1.5 py-0.5 text-xs font-medium hover:brightness-95', ROLE_STYLE[evidence.role])}
    >
      {Icon && <Icon className="size-3" aria-hidden />}
      {evidence.source_alias}
      {page && ` ${page}`}
    </button>
  )
}

/** Ghi chú đối chứng chéo: không suy ra đồng thuận khi chưa kiểm tra hoặc độc lập chưa xác định (mục 8.3). */
function crossCheckNote(claim: ClaimResult) {
  const others = claim.evidence.filter((e) => e.role === 'corroborating')
  if (claim.status === 'supported' && others.length) {
    return t('Nguồn khác cũng hỗ trợ ({0}), nhưng chưa xác định là độc lập.', { 0: others.map((e) => e.source_alias).join(', ') })
  }
  if (claim.cross_check_status === 'completed' && claim.status === 'supported') {
    return t('Đã đối chứng: không tìm thấy nguồn khác trong phạm vi đã xét. Điều này không có nghĩa là không tồn tại.')
  }
  return CROSS_CHECK[claim.cross_check_status]
}

export function ClaimCard({ claim }: { claim: ClaimResult }) {
  const [open, setOpen] = useState(false)
  const { openEvidence } = useViewer()
  const sides = [
    { title: t('Hỗ trợ'), items: claim.evidence.filter((e) => e.role !== 'contradicting') },
    { title: t('Mâu thuẫn'), items: claim.evidence.filter((e) => e.role === 'contradicting') },
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
        {claim.support_basis && SUPPORT_BASIS[claim.support_basis] && (
          <Pill className="bg-contested-soft text-contested" title={SUPPORT_BASIS[claim.support_basis]!.hint}>
            {SUPPORT_BASIS[claim.support_basis]!.label}
          </Pill>
        )}
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
          {t('Bằng chứng')} <ChevronDown className={clsx('size-3.5 transition-transform', open && 'rotate-180')} />
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
                      {e.modality && e.modality !== 'text' && ` · ${MODALITY[e.modality]}`}
                      {evidenceWhere(e) && ` · ${evidenceWhere(e)}`}
                    </span>
                    {e.source_deleted ? (
                      <span className="mt-1 block italic text-muted">{t('Nguồn đã bị xóa — nội dung đã ẩn.')}</span>
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
