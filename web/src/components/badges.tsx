import { AlertTriangle, Ban, CheckCheck, CircleCheck, CircleSlash, Clock, XCircle } from 'lucide-react'
import type { AnswerStatus, ClaimStatus, RunStatus, SourceStatus } from '../api'
import { ANSWER_STATUS, CLAIM_STATUS, RUN_STATUS, SOURCE_STATUS, isIngesting } from '../lib/labels'
import { Pill, Spinner } from './ui'

const CLAIM_STYLE: Record<ClaimStatus, string> = {
  supported: 'bg-supported-soft text-supported',
  corroborated: 'bg-corroborated-soft text-corroborated',
  contested: 'bg-contested-soft text-contested',
}

const CLAIM_ICON: Record<ClaimStatus, typeof CircleCheck> = {
  supported: CircleCheck,
  corroborated: CheckCheck,
  contested: AlertTriangle,
}

export function ClaimStatusBadge({ status }: { status: ClaimStatus }) {
  const Icon = CLAIM_ICON[status]
  return (
    <Pill className={CLAIM_STYLE[status]} title={CLAIM_STATUS[status].hint}>
      <Icon className="size-3.5" aria-hidden />
      {CLAIM_STATUS[status].label}
    </Pill>
  )
}

const SOURCE_STYLE: Record<SourceStatus, string> = {
  uploaded: 'bg-surface-2 text-muted',
  parsing: 'bg-accent-soft text-accent',
  chunking: 'bg-accent-soft text-accent',
  embedding: 'bg-accent-soft text-accent',
  publishing: 'bg-accent-soft text-accent',
  ready: 'bg-corroborated-soft text-corroborated',
  failed: 'bg-rejected-soft text-rejected',
  cancelled: 'bg-surface-2 text-muted',
  deleting: 'bg-surface-2 text-muted',
}

export function SourceStatusBadge({ status }: { status: SourceStatus }) {
  return (
    <Pill className={SOURCE_STYLE[status]}>
      {(isIngesting(status) || status === 'deleting') && <Spinner className="size-3" />}
      {SOURCE_STATUS[status]}
    </Pill>
  )
}

const RUN_STYLE: Record<RunStatus, string> = {
  queued: 'bg-surface-2 text-muted',
  running: 'bg-accent-soft text-accent',
  succeeded: 'bg-corroborated-soft text-corroborated',
  partial: 'bg-contested-soft text-contested',
  failed: 'bg-rejected-soft text-rejected',
  cancelled: 'bg-surface-2 text-muted',
}

const RUN_ICON: Partial<Record<RunStatus, typeof Clock>> = {
  queued: Clock,
  succeeded: CircleCheck,
  partial: AlertTriangle,
  failed: XCircle,
  cancelled: Ban,
}

export function RunStatusBadge({ status }: { status: RunStatus }) {
  const Icon = RUN_ICON[status]
  return (
    <Pill className={RUN_STYLE[status]}>
      {status === 'running' ? <Spinner className="size-3" /> : Icon && <Icon className="size-3.5" aria-hidden />}
      {RUN_STATUS[status]}
    </Pill>
  )
}

export function AnswerStatusBadge({ status }: { status: AnswerStatus }) {
  if (status === 'answered') return null
  return (
    <Pill className="bg-surface-2 text-fg" title={ANSWER_STATUS[status].hint}>
      <CircleSlash className="size-3.5" aria-hidden />
      {ANSWER_STATUS[status].label}
    </Pill>
  )
}
