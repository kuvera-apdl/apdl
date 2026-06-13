import { Star } from 'lucide-react'

import type { VariantConfig } from '@/api/types/flags'
import { formatPercent, variantShare } from '@/lib/format'
import { cn } from '@/lib/utils'

const SEGMENT_COLORS = [
  'bg-sky-500',
  'bg-violet-500',
  'bg-emerald-500',
  'bg-amber-500',
  'bg-rose-500',
  'bg-cyan-500',
]

interface VariantSplitBarProps {
  variants: VariantConfig[]
  defaultVariant: string
  className?: string
}

// Read-only weight split (plan §5.3.2): weights normalized to %, default
// variant starred.
export function VariantSplitBar({ variants, defaultVariant, className }: VariantSplitBarProps) {
  return (
    <div className={cn('space-y-2', className)}>
      <div className="flex h-2.5 w-full overflow-hidden rounded-full bg-muted">
        {variants.map((variant, index) => {
          const share = variantShare(variant, variants)
          if (share <= 0) return null
          return (
            <div
              key={variant.key}
              className={SEGMENT_COLORS[index % SEGMENT_COLORS.length]}
              style={{ width: `${share}%` }}
              title={`${variant.key}: ${formatPercent(share)}`}
            />
          )
        })}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-sm">
        {variants.map((variant, index) => (
          <li key={variant.key} className="flex items-center gap-1.5">
            <span
              className={cn('h-2 w-2 rounded-full', SEGMENT_COLORS[index % SEGMENT_COLORS.length])}
            />
            <code className="font-mono text-xs">{variant.key}</code>
            {variant.key === defaultVariant ? (
              <Star className="h-3 w-3 fill-amber-400 text-amber-400" aria-label="default variant" />
            ) : null}
            <span className="text-muted-foreground">
              {variant.weight} ({formatPercent(variantShare(variant, variants))})
            </span>
          </li>
        ))}
      </ul>
    </div>
  )
}
