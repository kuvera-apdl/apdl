import { cn } from '@/lib/utils'
import type { Changeset } from '@/api/types/codegen'

const ACTIVE = 'animate-pulse border-sky-200 bg-sky-100 text-sky-800 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-300'
const GATE = 'animate-pulse border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300'
const GOOD = 'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300'
const BAD = 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300'
const MUTED = 'border-transparent bg-muted text-muted-foreground'

const STATUS_STYLES: Record<string, string> = {
  queued: MUTED,
  cloning: ACTIVE,
  editing: ACTIVE,
  pushing: ACTIVE,
  pr_open: GATE,
  merged: GOOD,
  abandoned: MUTED,
  error: BAD,
}

const PR_STATUS_STYLES: Record<string, string> = {
  draft: GATE,
  open: ACTIVE,
  merged: GOOD,
  closed: MUTED,
}

const CI_STATUS_STYLES: Record<string, string> = {
  pending: ACTIVE,
  passed: GOOD,
  failed: BAD,
  unverified_external_ci: GATE,
}

function StatusPill({ status, styles }: { status: string; styles: Record<string, string> }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        styles[status] ?? 'bg-secondary text-secondary-foreground',
      )}
    >
      {status.replace(/_/g, ' ')}
    </span>
  )
}

export function ChangesetStatusPill({ status }: { status: Changeset['status'] }) {
  return <StatusPill status={status} styles={STATUS_STYLES} />
}

export function GitHubPRStatusPill({
  status,
}: {
  status: NonNullable<Changeset['github_pr_status']>
}) {
  return <StatusPill status={status} styles={PR_STATUS_STYLES} />
}

export function ExternalCIStatusPill({
  status,
}: {
  status: NonNullable<Changeset['external_ci_status']>
}) {
  return <StatusPill status={status} styles={CI_STATUS_STYLES} />
}
