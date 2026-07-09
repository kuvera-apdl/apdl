import { LOOP_TONE_CLASSES, loopStageMeta, type LoopStage } from '@/lib/loopStatus'
import { cn } from '@/lib/utils'

// The shared status pill for every loop surface. Supersedes the per-feature
// StatePill / RunStatusPill for anything speaking the loop vocabulary — pass a
// LoopStage and the color + label come from the single mapping.
export function LoopStatusPill({
  stage,
  pulse,
  className,
}: {
  stage: LoopStage
  pulse?: boolean
  className?: string
}) {
  const { label, tone } = loopStageMeta(stage)
  return (
    <span
      className={cn(
        'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
        LOOP_TONE_CLASSES[tone],
        pulse && 'animate-pulse',
        className,
      )}
    >
      {label}
    </span>
  )
}
