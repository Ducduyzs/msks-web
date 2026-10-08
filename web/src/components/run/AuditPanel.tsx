import clsx from 'clsx'
import { useState } from 'react'
import type { ClaimResult, ContextBlock, Provenance, RejectedClaim } from '../../api'
import { EVIDENCE_ROLE, num, pageLabel, rejectReason } from '../../lib/labels'

type Tab = 'rejected' | 'verification' | 'context' | 'provenance'

/** Chế độ kiểm toán (F8): claim bị loại, tín hiệu kiểm chứng, context, nguồn gốc lượt chạy. */
export function AuditPanel({
  claims,
  rejected,
  context,
  provenance,
}: {
  claims: ClaimResult[]
  rejected: RejectedClaim[]
  context: ContextBlock[]
  provenance?: Provenance
}) {
  const [tab, setTab] = useState<Tab>('rejected')
  const tabs: { key: Tab; label: string }[] = [
    { key: 'rejected', label: `Claim bị loại (${rejected.length})` },
    { key: 'verification', label: 'Tín hiệu kiểm chứng' },
    { key: 'context', label: `Context (${context.length})` },
    { key: 'provenance', label: 'Nguồn gốc lượt chạy' },
  ]
  return (
    <section className="card" aria-label="Kiểm toán">
      <div className="flex gap-1 overflow-x-auto border-b border-line px-2" role="tablist">
        {tabs.map((t) => (
          <button
            key={t.key}
            role="tab"
            aria-selected={tab === t.key}
            onClick={() => setTab(t.key)}
            className={clsx(
              'whitespace-nowrap border-b-2 px-3 py-2.5 text-sm',
              tab === t.key ? 'border-accent font-medium text-fg' : 'border-transparent text-muted hover:text-fg',
            )}
          >
            {t.label}
          </button>
        ))}
      </div>
      <div className="p-4" role="tabpanel">
        {tab === 'rejected' &&
          (rejected.length ? (
            <ul className="space-y-2">
              {rejected.map((r) => (
                <li key={r.id} className="rounded-md bg-rejected-soft/60 p-3 text-sm">
                  <p className="text-fg line-through decoration-rejected/60">{r.text}</p>
                  <p className="mt-1 text-xs text-rejected">
                    {rejectReason(r.reason)}
                    {r.support != null && ` · tín hiệu hỗ trợ tốt nhất ${num(r.support)}`}
                  </p>
                </li>
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted">Không có claim nào bị loại.</p>
          ))}

        {tab === 'verification' && (
          <div className="space-y-3">
            <p className="text-xs text-muted">
              Điểm NLI là tín hiệu của model, không phải xác suất đúng đã hiệu chuẩn. Độ tự tin của LLM là tự đánh giá, không dùng làm bảo đảm.
            </p>
            {claims.length ? (
              <div className="overflow-x-auto">
                <table className="w-full min-w-[560px] text-sm">
                  <thead className="text-left text-muted">
                    <tr>
                      <th className="py-1.5 pr-3 font-medium">Claim</th>
                      <th className="py-1.5 pr-3 font-medium">Evidence</th>
                      <th className="py-1.5 pr-3 text-right font-medium">Entail</th>
                      <th className="py-1.5 pr-3 text-right font-medium">Contra.</th>
                      <th className="py-1.5 text-right font-medium">LLM tự đánh giá</th>
                    </tr>
                  </thead>
                  <tbody className="divide-y divide-line">
                    {claims.flatMap((c) =>
                      c.evidence.map((e, i) => (
                        <tr key={e.id} className="align-top">
                          <td className="py-1.5 pr-3">{i === 0 ? c.text : ''}</td>
                          <td className="py-1.5 pr-3 whitespace-nowrap">
                            {e.source_alias} · {EVIDENCE_ROLE[e.role]}
                            <span className="block font-mono text-xs text-muted">{e.verifier_revision}</span>
                          </td>
                          <td className="py-1.5 pr-3 text-right tabular-nums">{num(e.support)}</td>
                          <td className="py-1.5 pr-3 text-right tabular-nums">{num(e.contradiction)}</td>
                          <td className="py-1.5 text-right tabular-nums">{i === 0 ? num(c.generator_confidence) : ''}</td>
                        </tr>
                      )),
                    )}
                  </tbody>
                </table>
              </div>
            ) : (
              <p className="text-sm text-muted">Chưa có claim nào qua kiểm chứng.</p>
            )}
          </div>
        )}

        {tab === 'context' && (
          <ol className="space-y-2">
            {context.map((b) => (
              <li key={b.id} className="rounded-md bg-surface-2 p-3 text-sm">
                <p className="label">
                  [{b.context_id}] {b.source_alias}
                  {pageLabel(b.page_start, b.page_end) && `, ${pageLabel(b.page_start, b.page_end)}`} · hạng {b.rank} · {b.token_count} token
                  {b.truncated && ' · đã cắt'}
                </p>
                {b.visible_text ? (
                  <p className="mt-1">{b.visible_text}</p>
                ) : (
                  <p className="mt-1 italic text-muted">Nội dung đã ẩn vì nguồn đã bị xóa.</p>
                )}
                <p className="mt-2 font-mono text-xs text-muted">
                  ce {num(b.trace.raw_ce_score, 3)} · sbert {num(b.trace.sbert_similarity, 3)} · rrf {num(b.trace.rrf_score, 4)} · utility{' '}
                  {num(b.trace.packing_utility, 3)}
                </p>
              </li>
            ))}
            {!context.length && <p className="text-sm text-muted">Chưa có context.</p>}
          </ol>
        )}

        {tab === 'provenance' &&
          (provenance ? (
            <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-1.5 text-sm">
              <Field label="Profile" value={provenance.profile} />
              <Field label="Cấu hình" value={provenance.config_hash} />
              <Field label="Mã nguồn" value={provenance.code_hash} />
              <Field label="EDAHR commit" value={provenance.edahr_commit ?? 'chưa pin'} />
              <Field label="Verifier" value={provenance.verifier_revision} />
              <Field label="Calibration" value={provenance.calibration_artifact ?? 'chưa có (thử nghiệm)'} />
              <Field label="Grouping" value={`v${provenance.grouping_version}`} />
              {Object.entries(provenance.models).map(([k, v]) => (
                <Field key={k} label={k} value={v} />
              ))}
              <dt className="text-muted">Snapshot nguồn</dt>
              <dd>
                <ul className="space-y-0.5 font-mono text-xs">
                  {provenance.source_snapshot.map((s) => (
                    <li key={s.source_id}>
                      {s.alias}: {s.document_revision_id} / {s.parse_revision_id}
                    </li>
                  ))}
                </ul>
              </dd>
            </dl>
          ) : (
            <p className="text-sm text-muted">Có sau khi lượt chạy hoàn tất.</p>
          ))}
      </div>
    </section>
  )
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <>
      <dt className="text-muted">{label}</dt>
      <dd className="font-mono break-all">{value}</dd>
    </>
  )
}
