import type { ClaimResult } from '../../api'
import { ClaimStatusBadge } from '../badges'
import { CitationChip } from '../ClaimCard'

/** Bảng đồng thuận / mâu thuẫn giữa các nguồn (mục 7.3, 11) — chỉ trong phạm vi nguồn đã xét. */
export function ConsensusTable({ claims }: { claims: ClaimResult[] }) {
  const rows = claims.filter((c) => c.evidence.length > 1 || c.status !== 'supported')
  if (!rows.length) return null
  return (
    <section className="card overflow-hidden">
      <div className="border-b border-line px-4 py-3">
        <h3 className="font-semibold">Đồng thuận và mâu thuẫn giữa các nguồn</h3>
        <p className="mt-0.5 text-xs text-muted">Chỉ trong phạm vi nguồn đã xét; không tìm thấy mâu thuẫn không có nghĩa là không tồn tại.</p>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full min-w-[560px] text-sm">
          <thead className="bg-surface-2 text-left text-muted">
            <tr>
              <th className="px-4 py-2 font-medium">Claim</th>
              <th className="px-4 py-2 font-medium">Trạng thái</th>
              <th className="px-4 py-2 font-medium">Hỗ trợ</th>
              <th className="px-4 py-2 font-medium">Mâu thuẫn</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-line">
            {rows.map((c) => (
              <tr key={c.id} className="align-top">
                <td className="px-4 py-3">{c.text}</td>
                <td className="px-4 py-3">
                  <ClaimStatusBadge status={c.status} />
                </td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {c.evidence.filter((e) => e.role !== 'contradicting').map((e) => (
                      <CitationChip key={e.id} evidence={e} />
                    ))}
                  </div>
                </td>
                <td className="px-4 py-3">
                  <div className="flex flex-wrap gap-1">
                    {c.evidence.filter((e) => e.role === 'contradicting').map((e) => (
                      <CitationChip key={e.id} evidence={e} />
                    ))}
                  </div>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </section>
  )
}
