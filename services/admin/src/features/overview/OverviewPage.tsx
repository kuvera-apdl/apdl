import { Link } from 'react-router-dom'

import { createFlagExampleCurl, listFlagsCurl } from '@/api/config'
import { SERVICE_DESCRIPTORS } from '@/api/health'
import type { FlagState } from '@/api/types/flags'
import { CurlButton } from '@/components/shared/CurlButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StatePill } from '@/components/shared/StatePill'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useLive } from '@/core/live'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useFlagsQuery } from '@/features/flags/hooks'
import { ServiceHealthCard } from '@/features/system/ServiceHealthCard'
import { useServiceHealthQuery } from '@/features/system/hooks'
import { useNow } from '@/lib/hooks'
import { isPastDate, parseServerDate } from '@/lib/format'

const FLAG_STATES: FlagState[] = ['draft', 'active', 'disabled', 'archived']

function HealthStrip() {
  const queries = {
    ingestion: useServiceHealthQuery('ingestion'),
    config: useServiceHealthQuery('config'),
    query: useServiceHealthQuery('query'),
    agents: useServiceHealthQuery('agents'),
  }
  return (
    <div className="grid gap-4 sm:grid-cols-2 xl:grid-cols-4">
      {SERVICE_DESCRIPTORS.map(({ service, label }) => (
        <ServiceHealthCard
          key={service}
          label={label}
          result={queries[service].data}
          isLoading={queries[service].isPending}
          linkTo="/system/health"
        />
      ))}
    </div>
  )
}

function FlagsSummaryCard() {
  const { active } = useWorkspace()
  const flagsQuery = useFlagsQuery()
  const conn = active ? serviceConnection(active, 'config') : null

  const flags = flagsQuery.data?.flags ?? []
  const countsByState = Object.fromEntries(
    FLAG_STATES.map((state) => [state, flags.filter((flag) => flag.state === state).length]),
  ) as Record<FlagState, number>
  const overdueReviews = flags.filter(
    (flag) => flag.state !== 'archived' && isPastDate(flag.review_by),
  ).length
  const recent = [...flags]
    .sort(
      (a, b) =>
        (parseServerDate(b.updated_at)?.getTime() ?? 0) -
        (parseServerDate(a.updated_at)?.getTime() ?? 0),
    )
    .slice(0, 5)

  return (
    <Card className="lg:col-span-2">
      <CardHeader className="flex-row items-start justify-between space-y-0">
        <div className="space-y-1.5">
          <CardTitle>Feature flags</CardTitle>
          <CardDescription>State of the project's flags, live over SSE.</CardDescription>
        </div>
        {conn ? <CurlButton spec={listFlagsCurl(conn)} title="List flags" /> : null}
      </CardHeader>
      <CardContent className="space-y-4">
        {flagsQuery.isPending ? (
          <div className="space-y-2">
            <Skeleton className="h-8 w-full" />
            <Skeleton className="h-8 w-2/3" />
          </div>
        ) : flagsQuery.error ? (
          <ErrorState error={flagsQuery.error} onRetry={() => void flagsQuery.refetch()} />
        ) : flags.length === 0 ? (
          <EmptyState
            title="No flags yet"
            description="Create the first flag via the API — the console's create form lands in the flag-write phase."
          >
            {conn ? <CurlButton spec={createFlagExampleCurl(conn)} title="Create a flag" /> : null}
          </EmptyState>
        ) : (
          <>
            <div className="flex flex-wrap gap-2">
              {FLAG_STATES.map((state) => (
                <Link
                  key={state}
                  to={`/flags?state=${state}${state === 'archived' ? '&archived=1' : ''}`}
                  className="flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
                >
                  <StatePill state={state} />
                  <span className="font-semibold tabular-nums">{countsByState[state]}</span>
                </Link>
              ))}
              <Link
                to="/flags/hygiene"
                className="flex items-center gap-2 rounded-md border px-3 py-1.5 text-sm hover:bg-accent"
              >
                <span className="text-muted-foreground">review overdue</span>
                <span className="font-semibold tabular-nums">{overdueReviews}</span>
              </Link>
            </div>
            <div className="space-y-1">
              <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                Recently updated
              </p>
              <ul className="divide-y">
                {recent.map((flag) => (
                  <li key={flag.key}>
                    <Link
                      to={`/flags/${encodeURIComponent(flag.key)}`}
                      className="flex items-center justify-between gap-3 py-2 hover:bg-accent/40"
                    >
                      <span className="flex min-w-0 items-center gap-2">
                        <code className="truncate font-mono text-sm">{flag.key}</code>
                        <StatePill state={flag.state} />
                      </span>
                      <span className="shrink-0 text-xs text-muted-foreground">
                        v{flag.version} · <RelativeTime value={flag.updated_at} />
                      </span>
                    </Link>
                  </li>
                ))}
              </ul>
            </div>
          </>
        )}
      </CardContent>
    </Card>
  )
}

const STREAM_LABELS = {
  idle: 'Offline',
  connecting: 'Connecting…',
  open: 'Connected',
  reconnecting: 'Reconnecting…',
} as const

function LiveStreamCard() {
  const { state } = useLive()
  const now = useNow(1000)
  const lastEventSeconds =
    state.lastEventAt !== null ? Math.max(0, Math.round((now - state.lastEventAt) / 1000)) : null

  return (
    <Card>
      <CardHeader>
        <CardTitle>Realtime stream</CardTitle>
        <CardDescription>GET /v1/stream — flags reach SDKs over this channel.</CardDescription>
      </CardHeader>
      <CardContent>
        <dl className="space-y-2 text-sm">
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Status</dt>
            <dd className="font-medium">{STREAM_LABELS[state.status]}</dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Last event</dt>
            <dd className="tabular-nums">
              {lastEventSeconds !== null ? `${lastEventSeconds}s ago` : '—'}
            </dd>
          </div>
          <div className="flex justify-between">
            <dt className="text-muted-foreground">Reconnects</dt>
            <dd className="tabular-nums">{state.reconnects}</dd>
          </div>
        </dl>
        <p className="mt-3 text-xs text-muted-foreground">
          Server heartbeats every 35s; the console reconnects with backoff if the stream goes
          quiet for 90s.
        </p>
      </CardContent>
    </Card>
  )
}

export function OverviewPage() {
  return (
    <div className="space-y-6">
      <PageHeader
        title="Overview"
        description="Is the Loop alive — service health, flag state, and live distribution at a glance."
      />
      <HealthStrip />
      <div className="grid items-start gap-4 lg:grid-cols-3">
        <FlagsSummaryCard />
        <LiveStreamCard />
      </div>
    </div>
  )
}
