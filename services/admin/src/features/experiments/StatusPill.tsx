// Canonical experiment statuses get semantic colors.
import { cn } from '@/lib/utils'

const KNOWN_STATUS_STYLES: Record<string, string> = {
  draft:
    'border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300',
  scheduled:
    'border-violet-200 bg-violet-100 text-violet-800 dark:border-violet-900 dark:bg-violet-950/60 dark:text-violet-300',
  running:
    'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  completed:
    'border-sky-200 bg-sky-100 text-sky-800 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-300',
  stopped:
    'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
}

export const KNOWN_EXPERIMENT_STATUSES = Object.keys(KNOWN_STATUS_STYLES)

export function ExperimentStatusPill({ status }: { status: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        KNOWN_STATUS_STYLES[status] ?? 'bg-secondary text-secondary-foreground',
      )}
    >
      {status || 'unset'}
    </span>
  )
}
