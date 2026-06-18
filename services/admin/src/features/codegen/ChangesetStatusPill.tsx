import { cn } from '@/lib/utils'

const ACTIVE = 'animate-pulse border-sky-200 bg-sky-100 text-sky-800 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-300'
const GATE = 'animate-pulse border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300'
const GOOD = 'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300'
const BAD = 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300'
const MUTED = 'border-transparent bg-muted text-muted-foreground'

const STATUS_STYLES: Record<string, string> = {
  queued: MUTED,
  cloning: ACTIVE,
  editing: ACTIVE,
  testing: ACTIVE,
  tests_failed: BAD,
  pushing: ACTIVE,
  pr_open: GATE,
  ci_running: ACTIVE,
  ci_failed: BAD,
  ci_passed: GOOD,
  waiting_approval: GATE,
  merged: GOOD,
  abandoned: MUTED,
  error: BAD,
}

export function ChangesetStatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        STATUS_STYLES[status] ?? 'bg-secondary text-secondary-foreground',
      )}
    >
      {status.replace(/_/g, ' ')}
    </span>
  )
}
