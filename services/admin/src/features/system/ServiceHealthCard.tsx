import { RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import {
  healthLevel,
  type HealthLevel,
  type ProbeResult,
  type ServiceHealth,
} from '@/api/health'
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

function configSummary(result: ServiceHealth): string {
  const ready = result.ready
  if (!ready) return 'readiness: unknown'
  if (ready.error) return `readiness error: ${ready.error}`
  if (!isRecord(ready.body)) return 'readiness: unknown'

  const checks = isRecord(ready.body.checks) ? ready.body.checks : null
  const sse = isRecord(ready.body.sse) ? ready.body.sse : null
  const postgres = typeof checks?.postgres === 'string' ? checks.postgres : 'unknown'
  const redis = typeof checks?.redis === 'string' ? checks.redis : 'unknown'
  const additionalFailures = checks
    ? Object.entries(checks).flatMap(([name, status]) => {
        if (name === 'postgres' || name === 'redis' || status === 'ready') return []
        return [`${name}: ${typeof status === 'string' ? status : 'unknown'}`]
      })
    : []
  const activeConnections =
    typeof sse?.active_connections === 'number' &&
    Number.isInteger(sse.active_connections) &&
    sse.active_connections >= 0
      ? String(sse.active_connections)
      : 'unknown'
  return [
    `pg: ${postgres}`,
    `redis: ${redis}`,
    `sse: ${activeConnections}`,
    ...additionalFailures,
  ].join(' · ')
}

function summaryLine(result: ServiceHealth): string {
  const body = isRecord(result.health.body) ? result.health.body : null
  if (result.service === 'config') return configSummary(result)
  if (result.ready) {
    const readyBody = isRecord(result.ready.body) ? result.ready.body : null
    const status = typeof readyBody?.status === 'string' ? readyBody.status : null
    return `ready: ${status ?? result.ready.error ?? 'unknown'}`
  }
  const status = typeof body?.status === 'string' ? body.status : null
  return `status: ${status ?? result.health.error ?? 'unknown'}`
}

function ProbeDetails({ label, probe }: { label: '/health' | '/ready'; probe: ProbeResult }) {
  const status =
    probe.status !== null
      ? `HTTP ${probe.status} · ${formatMs(probe.latencyMs)}`
      : (probe.error ?? 'unreachable')
  return (
    <section aria-label={`${label} response`} className="space-y-1">
      <p className="text-xs font-medium">
        {label} <span className="font-normal text-muted-foreground">· {status}</span>
      </p>
      {probe.body !== null ? (
        <pre className="max-h-40 overflow-auto rounded-md bg-muted p-2 font-mono text-xs">
          {JSON.stringify(probe.body, null, 2)}
        </pre>
      ) : (
        <p className="rounded-md bg-muted p-2 text-xs text-muted-foreground">
          {probe.error ? `No JSON body · ${probe.error}` : 'No JSON response body'}
        </p>
      )}
    </section>
  )
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
            {detailed ? (
              <div className="mt-2 space-y-3">
                <ProbeDetails label="/health" probe={result.health} />
                {result.ready ? <ProbeDetails label="/ready" probe={result.ready} /> : null}
              </div>
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
