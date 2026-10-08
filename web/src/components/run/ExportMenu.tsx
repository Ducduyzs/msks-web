import { Download } from 'lucide-react'
import { useState } from 'react'
import { api, type ExportFormat } from '../../api'
import { features } from '../../lib/features'
import { ErrorBox, Spinner } from '../ui'

// MVP: Markdown/JSON; DOCX/PDF thuộc G4 (mục 1.4, 16).
const FORMATS: { format: ExportFormat; label: string }[] = [
  { format: 'md', label: 'Markdown' },
  { format: 'json', label: 'JSON' },
  ...(features.synthesis
    ? [
        { format: 'docx' as const, label: 'DOCX' },
        { format: 'pdf' as const, label: 'PDF' },
      ]
    : []),
]

export function ExportMenu({ runId }: { runId: string }) {
  const [busy, setBusy] = useState<ExportFormat | null>(null)
  const [error, setError] = useState<unknown>(null)

  async function download(format: ExportFormat) {
    setBusy(format)
    setError(null)
    try {
      const blob = await api.exportRun(runId, format)
      const url = URL.createObjectURL(blob)
      const link = document.createElement('a')
      link.href = url
      link.download = `msks-${runId}.${format}`
      link.click()
      setTimeout(() => URL.revokeObjectURL(url), 0)
    } catch (err) {
      setError(err)
    } finally {
      setBusy(null)
    }
  }

  return (
    <div className="space-y-2">
      <div className="flex flex-wrap gap-1.5">
        {FORMATS.map(({ format, label }) => (
          <button key={format} className="btn btn-outline px-2.5 py-1.5" onClick={() => download(format)} disabled={busy !== null}>
            {busy === format ? <Spinner /> : <Download className="size-3.5" />}
            {label}
          </button>
        ))}
      </div>
      <p className="text-xs text-muted">Bản xuất dựng từ claim và evidence đã lưu, không gọi LLM viết lại.</p>
      {error != null && <ErrorBox error={error} />}
    </div>
  )
}
