import { SERVICE_DESCRIPTORS } from '@/api/health'
import { PageHeader } from '@/components/shared/PageHeader'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useLive } from '@/core/live'
import { ServiceHealthCard } from '@/features/system/ServiceHealthCard'
import { useServiceHealthQuery } from '@/features/system/hooks'
import { useNow } from '@/lib/hooks'

const STREAM_LABELS = {
  idle: 'Offline',
  connecting: 'Connecting…',
  open: 'Connected',
  reconnecting: 'Reconnecting…',
} as const

function SseCard() {
  const { state } = useLive()
  const now = useNow(1000)
  const lastEventSeconds =
    state.lastEventAt !== null ? Math.max(0, Math.round((now - state.lastEventAt) / 1000)) : null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Console SSE connection</CardTitle>
        <CardDescription>This console's own /v1/stream subscription.</CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted-foreground">State</dt>
            <dd className="font-medium">{STREAM_LABELS[state.status]}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Last event / heartbeat</dt>
            <dd className="tabular-nums">
              {lastEventSeconds !== null ? `${lastEventSeconds}s ago` : '—'}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Reconnects this session</dt>
            <dd className="tabular-nums">{state.reconnects}</dd>
          </div>
        </dl>
      </CardContent>
    </Card>
  )
}

export function HealthPage() {
  const queries = {
    ingestion: useServiceHealthQuery('ingestion'),
    config: useServiceHealthQuery('config'),
    query: useServiceHealthQuery('query'),
    agents: useServiceHealthQuery('agents'),
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="System health"
        description="Raw /health and /ready responses from each service, polled every 10s."
      />
      <div className="grid items-start gap-4 lg:grid-cols-2">
        {SERVICE_DESCRIPTORS.map(({ service, label }) => (
          <ServiceHealthCard
            key={service}
            label={label}
            result={queries[service].data}
            isLoading={queries[service].isPending}
            onRefresh={() => void queries[service].refetch()}
            detailed
          />
        ))}
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <SseCard />
      </div>
    </div>
  )
}
