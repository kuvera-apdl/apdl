import type { ReactNode } from 'react'

import { cn } from '@/lib/utils'

export interface Evidence {
  label: string
  value: ReactNode
  // Optional tone for the value — 'success'/'danger' color a lift or a breach.
  tone?: 'success' | 'danger' | 'muted'
}

const VALUE_TONE: Record<NonNullable<Evidence['tone']>, string> = {
  success: 'text-emerald-600 dark:text-emerald-400',
  danger: 'text-red-600 dark:text-red-400',
  muted: 'text-muted-foreground',
}

// Inline evidence: a compact, wrapping row of label→value facts. The reusable
// way every loop surface shows the numbers behind a decision or an outcome
// (Decide cards, experiment thread hub, Learn verdicts) without a table.
export function EvidenceRow({ items, className }: { items: Evidence[]; className?: string }) {
  if (items.length === 0) return null
  return (
    <div className={cn('flex flex-wrap gap-x-4 gap-y-1 text-sm text-muted-foreground', className)}>
      {items.map((item, index) => (
        <span key={`${item.label}-${index}`} className="inline-flex items-center gap-1">
          <span>{item.label}</span>
          <span className={cn('font-medium tabular-nums text-foreground', item.tone && VALUE_TONE[item.tone])}>
            {item.value}
          </span>
        </span>
      ))}
    </div>
  )
}
