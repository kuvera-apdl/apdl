import { RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { healthLevel, type HealthLevel, type ServiceHealth } from '@/api/health'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { formatMs } from '@/lib/format'
import { cn } from '@/lib/utils'

const LEVEL_STYLES: Record<HealthLevel, string> = {
  ok: 'bg-emerald-500',
  degraded: 'bg-amber-500',
  unreachable: 'bg-destructive',
}

interface ServiceHealthCardProps {
  label: string
  result: ServiceHealth | undefined
  isLoading: boolean
  linkTo?: string
  onRefresh?: () => void
  detailed?: boolean
}

function summaryLine(result: ServiceHealth): string {
  const body = result.health.body as Record<string, unknown> | null
  if (result.service === 'config' && body) {
    return `pg: ${String(body.postgres ?? '?')} · redis: ${String(body.redis ?? '?')} · sse: ${String(body.sse_connections ?? '?')}`
  }
  if (result.ready) {
    const readyBody = result.ready.body as { status?: unknown } | null
    return `ready: ${String(readyBody?.status ?? (result.ready.error ?? 'unknown'))}`
  }
  return `status: ${String(body?.status ?? (result.health.error ?? 'unknown'))}`
}

export function ServiceHealthCard({
  label,
  result,
  isLoading,
  linkTo,
  onRefresh,
  detailed = false,
}: ServiceHealthCardProps) {
  const level = result ? healthLevel(result) : null

  const body = (
    <Card className={cn(linkTo && 'transition-colors hover:border-foreground/20')}>
      <CardContent className="space-y-1.5 p-4">
        <div className="flex items-center justify-between gap-2">
          <span className="flex items-center gap-2 font-medium">
            <span
              className={cn('h-2 w-2 rounded-full', level ? LEVEL_STYLES[level] : 'bg-muted-foreground/40')}
            />
            {label}
          </span>
          <span className="flex items-center gap-1">
            {level ? (
              <Badge variant={level === 'ok' ? 'secondary' : 'destructive'}>{level}</Badge>
            ) : null}
            {onRefresh ? (
              <Button
                variant="ghost"
                size="icon"
                className="h-6 w-6"
                onClick={onRefresh}
                aria-label={`Refresh ${label} health`}
              >
                <RefreshCw className="h-3.5 w-3.5" />
              </Button>
            ) : null}
          </span>
        </div>
        {isLoading && !result ? (
          <Skeleton className="h-4 w-3/4" />
        ) : result ? (
          <>
            <p className="text-xs text-muted-foreground">
              {result.health.status !== null
                ? `HTTP ${result.health.status} · ${formatMs(result.health.latencyMs)}`
                : (result.health.error ?? 'unreachable')}
            </p>
            <p className="truncate text-xs text-muted-foreground" title={summaryLine(result)}>
              {summaryLine(result)}
            </p>
            {detailed && result.health.body !== null ? (
              <pre className="mt-2 max-h-40 overflow-auto rounded-md bg-muted p-2 font-mono text-xs">
                {JSON.stringify(result.health.body, null, 2)}
              </pre>
            ) : null}
          </>
        ) : (
          <p className="text-xs text-muted-foreground">No data.</p>
        )}
      </CardContent>
    </Card>
  )

  return linkTo ? <Link to={linkTo}>{body}</Link> : body
}
