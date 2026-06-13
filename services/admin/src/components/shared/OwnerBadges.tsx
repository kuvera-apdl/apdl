import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { initials } from '@/lib/format'

const VISIBLE_OWNERS = 3

export function OwnerBadges({ owners }: { owners: string[] }) {
  if (owners.length === 0) return <span className="text-muted-foreground">—</span>
  const visible = owners.slice(0, VISIBLE_OWNERS)
  const overflow = owners.length - visible.length
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex items-center -space-x-1.5">
          {visible.map((owner) => (
            <span
              key={owner}
              className="inline-flex h-6 w-6 items-center justify-center rounded-full border bg-secondary text-[10px] font-medium text-secondary-foreground"
            >
              {initials(owner)}
            </span>
          ))}
          {overflow > 0 ? (
            <span className="inline-flex h-6 w-6 items-center justify-center rounded-full border bg-muted text-[10px] text-muted-foreground">
              +{overflow}
            </span>
          ) : null}
        </span>
      </TooltipTrigger>
      <TooltipContent>{owners.join(', ')}</TooltipContent>
    </Tooltip>
  )
}
