import { ChevronRight } from 'lucide-react'
import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

// A styled native <details> disclosure — the reusable "dropdown" for
// expandable sections (agent inspector panels, config blocks, anywhere a
// labeled collapsible is wanted). Native <details> keeps it accessible and
// keyboard-toggleable with no JS state.
export function Disclosure({
  summary,
  children,
  defaultOpen,
  count,
  trailing,
  className,
}: {
  summary: ReactNode
  children: ReactNode
  defaultOpen?: boolean
  count?: number
  trailing?: ReactNode
  className?: string
}) {
  return (
    <details open={defaultOpen} className={cn('group rounded-md border', className)}>
      <summary className="flex cursor-pointer list-none items-center gap-2 px-3 py-2 text-sm hover:bg-muted/50 [&::-webkit-details-marker]:hidden">
        <ChevronRight className="h-4 w-4 shrink-0 text-muted-foreground transition-transform group-open:rotate-90" />
        <span className="min-w-0 flex-1">{summary}</span>
        {typeof count === 'number' ? (
          <span className="shrink-0 text-xs tabular-nums text-muted-foreground">{count}</span>
        ) : null}
        {trailing ? <span className="shrink-0">{trailing}</span> : null}
      </summary>
      <div className="border-t px-3 py-2.5">{children}</div>
    </details>
  )
}
