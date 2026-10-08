import type { RunMetrics } from '../../api'
import { num, pct } from '../../lib/labels'

interface Row {
  label: string
  value: string
  hint: string
  warn?: boolean
}

/** Chỉ số mục 8.4 — tín hiệu verifier, không phải xác suất đáp án sai. `—` khi mẫu số bằng 0. */
export function MetricsPanel({ metrics }: { metrics: RunMetrics }) {
  const rows: Row[] = [
    {
      label: 'Claim giữ lại / sinh ra',
      value: `${metrics.accepted_count} / ${metrics.generated_count}`,
      hint: 'Số claim qua kiểm chứng trên số claim sinh hợp lệ.',
    },
    {
      label: 'Tỉ lệ sống sót',
      value: pct(metrics.citation_survival_rate),
      hint: 'accepted / generated. Luôn đọc cùng số đếm ở trên.',
    },
    {
      label: 'Claim không đạt',
      value: pct(metrics.unsupported_generated_rate),
      hint: 'Claim không đạt policy / claim sinh hợp lệ — không bị che bởi bước lọc.',
    },
    {
      label: 'Rủi ro quy kết (trước lọc)',
      value: num(metrics.primary_attribution_risk_generated),
      hint: 'primary_attribution_risk_generated: tín hiệu verifier trên evidence chính, không phải xác suất sai.',
    },
    {
      label: 'Rủi ro quy kết (sau lọc)',
      value: num(metrics.primary_attribution_risk_accepted),
      hint: 'primary_attribution_risk_accepted: chỉ tính trên claim được giữ lại.',
    },
    {
      label: 'Có đối chứng độc lập',
      value: pct(metrics.corroboration_rate),
      hint: 'CORROBORATED / accepted, chỉ trong phạm vi nguồn được xét.',
    },
    {
      label: 'Có mâu thuẫn',
      value: pct(metrics.contest_rate),
      hint: 'CONTESTED / accepted, chỉ trong phạm vi nguồn được xét.',
      warn: (metrics.contest_rate ?? 0) > 0,
    },
    {
      label: 'Đối chứng hoàn tất',
      value: pct(metrics.cross_check_completion_rate),
      hint: 'Claim đã kiểm tra đủ ngân sách tìm kiếm / accepted. Phân biệt "chưa kiểm tra" với "không tìm thấy".',
    },
    {
      label: 'Tập trung nguồn',
      value: pct(metrics.source_concentration),
      hint: 'Tỉ lệ claim lớn nhất có evidence chính ở một nhóm nguồn.',
    },
    {
      label: 'Nhóm nguồn được trích',
      value: String(metrics.origin_groups_cited),
      hint: 'Số nhóm nguồn gốc — không tự gọi là số nguồn độc lập.',
    },
    { label: 'Lỗi JSON của mô hình', value: String(metrics.json_error_count), hint: 'Số lượt đầu ra sai hợp đồng.', warn: metrics.json_error_count > 0 },
    { label: 'Token context', value: metrics.context_tokens.toLocaleString('vi-VN'), hint: 'Đếm bằng bộ đếm của provider.' },
  ]
  const total = metrics.latency_ms.total
  return (
    <div>
      <dl className="divide-y divide-line">
        {rows.map((row) => (
          <div key={row.label} className="flex items-baseline justify-between gap-3 py-2" title={row.hint}>
            <dt className="text-sm text-muted">{row.label}</dt>
            <dd className={`text-sm font-semibold tabular-nums ${row.warn ? 'text-contested' : ''}`}>{row.value}</dd>
          </div>
        ))}
      </dl>
      {total !== undefined && (
        <details className="mt-2 text-sm">
          <summary className="cursor-pointer text-muted">Độ trễ {(total / 1000).toFixed(1)} s</summary>
          <dl className="mt-2 grid grid-cols-[1fr_auto] gap-x-4 gap-y-1 text-xs">
            {Object.entries(metrics.latency_ms)
              .filter(([key]) => key !== 'total')
              .map(([key, value]) => (
                <div key={key} className="contents">
                  <dt className="text-muted">{key}</dt>
                  <dd className="tabular-nums">{value} ms</dd>
                </div>
              ))}
          </dl>
        </details>
      )}
    </div>
  )
}
