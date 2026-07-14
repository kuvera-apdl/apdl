import type { ReactNode } from 'react'

// A small labeled section header used to group content within a purpose page
// (e.g. "Waiting on you", "Running", "Recent verdicts"). Lighter than
// PageHeader — for sections inside a page, not the page title.
export function SectionHeading({
  title,
  count,
  description,
  actions,
}: {
  title: ReactNode
  count?: number
  description?: ReactNode
  actions?: ReactNode
}) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-2">
      <div className="flex items-baseline gap-2">
        <h2 className="text-sm font-medium">{title}</h2>
        {typeof count === 'number' ? (
          <span className="text-xs tabular-nums text-muted-foreground">{count}</span>
        ) : null}
        {description ? <span className="text-xs text-muted-foreground">{description}</span> : null}
      </div>
      {actions ? <div className="flex items-center gap-2">{actions}</div> : null}
    </div>
  )
}
