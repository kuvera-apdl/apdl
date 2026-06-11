import { Link } from 'react-router-dom'

import { createFlagExampleCurl, listFlagsCurl } from '@/api/config'
import { SERVICE_DESCRIPTORS } from '@/api/health'
import { countEvents, timeseriesEvents } from '@/api/query'
import type { FlagState } from '@/api/types/flags'
import { CurlButton } from '@/components/shared/CurlButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StatePill } from '@/components/shared/StatePill'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { useQuery } from '@tanstack/react-query'

import { runStatus } from '@/api/agents'
import { TERMINAL_RUN_STATUSES } from '@/api/schemas/agents'
import { useLive } from '@/core/live'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { loadTrackedRuns } from '@/features/agents/runHistory'
import { RunStatusPill } from '@/features/agents/RunStatusPill'
import { TimeseriesChart } from '@/features/analytics/charts'
import { COMMON_EVENTS, lastDays } from '@/features/analytics/selectorModel'
import { useAnalyticsQuery } from '@/features/analytics/useAnalyticsQuery'
import { useExperimentsQuery } from '@/features/experiments/hooks'
import { ExperimentStatusPill } from '@/features/experiments/StatusPill'
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
          <EmptyState title="No flags yet" description="Create your first flag to start the Loop.">
            <Button size="sm" asChild>
              <Link to="/flags/new">New flag</Link>
            </Button>
            {conn ? <CurlButton spec={createFlagExampleCurl(conn)} title="Create via API" /> : null}
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

function ThroughputCard() {
  const { projectId } = useWorkspace()
  const tsBody = projectId
    ? {
        project_id: projectId,
        ...lastDays(2),
        selector: { event_name: '$pageview', filters: [] },
        interval: '1 HOUR' as const,
      }
    : null
  const tsQuery = useAnalyticsQuery('overview-throughput', tsBody, timeseriesEvents)
  const countBody = projectId
    ? {
        project_id: projectId,
        ...lastDays(1),
        selectors: COMMON_EVENTS.map((event) => ({ event_name: event, filters: [] })),
      }
    : null
  const countQuery = useAnalyticsQuery('overview-counts', countBody, countEvents)

  const totalToday = countQuery.data?.total_events ?? null
  const buckets = tsQuery.data?.buckets ?? []

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>Event throughput</CardTitle>
        <CardDescription>
          {totalToday !== null
            ? `${totalToday.toLocaleString()} events today across known event names · hourly $pageview below`
            : 'Hourly $pageview volume (the API has no match-all selector yet — gap G4).'}
        </CardDescription>
      </CardHeader>
      <CardContent>
        {tsQuery.isPending ? (
          <Skeleton className="h-48 w-full" />
        ) : tsQuery.error ? (
          <ErrorState error={tsQuery.error} onRetry={() => void tsQuery.refetch()} />
        ) : buckets.length === 0 && (totalToday === null || totalToday === 0) ? (
          <EmptyState title="No events in the last 24h" description="Is the SDK wired up?">
            <Link to="/settings/verify" className="text-sm font-medium underline underline-offset-4">
              Verify your integration →
            </Link>
          </EmptyState>
        ) : (
          <TimeseriesChart buckets={buckets} mode="bar" />
        )}
      </CardContent>
    </Card>
  )
}

function daysSince(date: string): number | null {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) return null
  const start = new Date(`${date}T00:00:00`)
  if (Number.isNaN(start.getTime())) return null
  return Math.max(0, Math.floor((Date.now() - start.getTime()) / 86_400_000))
}

function ExperimentsCard() {
  const experimentsQuery = useExperimentsQuery()
  const experiments = experimentsQuery.data?.experiments ?? []
  const running = experiments.filter((experiment) => experiment.status === 'running')

  return (
    <Card>
      <CardHeader>
        <CardTitle>Experiments</CardTitle>
        <CardDescription>
          {running.length} running · {experiments.length} total
        </CardDescription>
      </CardHeader>
      <CardContent>
        {experimentsQuery.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : running.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            Nothing running.{' '}
            <Link to="/experiments" className="font-medium underline underline-offset-4">
              All experiments →
            </Link>
          </p>
        ) : (
          <ul className="divide-y">
            {running.slice(0, 5).map((experiment) => {
              const days = daysSince(experiment.start_date)
              return (
                <li key={experiment.key}>
                  <Link
                    to={`/experiments/${encodeURIComponent(experiment.key)}`}
                    className="flex items-center justify-between gap-2 py-2 hover:bg-accent/40"
                  >
                    <span className="flex min-w-0 items-center gap-2">
                      <code className="truncate font-mono text-sm">{experiment.key}</code>
                      <ExperimentStatusPill status={experiment.status} />
                    </span>
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {days !== null ? `day ${days + 1}` : ''}
                    </span>
                  </Link>
                </li>
              )
            })}
          </ul>
        )}
      </CardContent>
    </Card>
  )
}

function AgentsCard() {
  const { active } = useWorkspace()
  const latest = active ? (loadTrackedRuns(active.id)[0] ?? null) : null

  const statusQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run-overview', latest?.run_id ?? 'none'],
    enabled: active !== null && latest !== null,
    queryFn: ({ signal }) => runStatus(serviceConnection(active!, 'agents'), latest!.run_id, { signal }),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      return status && TERMINAL_RUN_STATUSES.has(status) ? false : 10_000
    },
  })

  const status = statusQuery.data?.status ?? latest?.last_status ?? null
  const awaitingApproval = status === 'waiting_approval'

  return (
    <Card className={awaitingApproval ? 'border-amber-400 dark:border-amber-700' : undefined}>
      <CardHeader>
        <CardTitle>Agents</CardTitle>
        <CardDescription>
          {latest ? 'Latest run triggered from this browser.' : 'The autonomous loop, on demand.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3 text-sm">
        {awaitingApproval && latest ? (
          <Link
            to={`/agents/runs/${encodeURIComponent(latest.run_id)}`}
            className="block rounded-md border border-amber-400 bg-amber-50 p-3 font-medium text-amber-900 dark:bg-amber-950/40 dark:text-amber-200"
          >
            ⏳ Awaiting your approval — review the run →
          </Link>
        ) : null}
        {latest ? (
          <Link
            to={`/agents/runs/${encodeURIComponent(latest.run_id)}`}
            className="flex items-center justify-between gap-2 hover:underline"
          >
            <code className="font-mono text-xs">{latest.run_id.slice(0, 8)}…</code>
            {status ? <RunStatusPill status={status} /> : null}
          </Link>
        ) : (
          <p className="text-muted-foreground">No runs yet.</p>
        )}
        <Link to="/agents/trigger" className="block text-sm font-medium underline underline-offset-4">
          Trigger a run →
        </Link>
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
        <ThroughputCard />
        <ExperimentsCard />
      </div>
      <div className="grid items-start gap-4 lg:grid-cols-3">
        <FlagsSummaryCard />
        <div className="space-y-4">
          <AgentsCard />
          <LiveStreamCard />
        </div>
      </div>
    </div>
  )
}
