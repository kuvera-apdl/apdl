// Semantic flag-state tokens (plan §4.6): draft = slate, active = green,
// disabled = amber, archived = gray strikethrough.
import type { FlagState } from '@/api/types/flags'
import { cn } from '@/lib/utils'

const STATE_STYLES: Record<FlagState, string> = {
  draft:
    'border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300',
  active:
    'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  disabled:
    'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
  archived: 'border-transparent bg-muted text-muted-foreground line-through',
}

export function StatePill({ state, className }: { state: FlagState; className?: string }) {
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        STATE_STYLES[state],
        className,
      )}
    >
      {state}
    </span>
  )
}
