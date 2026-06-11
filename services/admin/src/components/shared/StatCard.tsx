import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { Card, CardContent } from '@/components/ui/card'
import { cn } from '@/lib/utils'

interface StatCardProps {
  label: string
  value: ReactNode
  hint?: ReactNode
  to?: string
  className?: string
}

export function StatCard({ label, value, hint, to, className }: StatCardProps) {
  const body = (
    <Card className={cn('h-full', to && 'transition-colors hover:border-foreground/20', className)}>
      <CardContent className="space-y-1 p-4">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
        <div className="text-2xl font-semibold tabular-nums">{value}</div>
        {hint ? <div className="text-xs text-muted-foreground">{hint}</div> : null}
      </CardContent>
    </Card>
  )
  return to ? <Link to={to}>{body}</Link> : body
}
