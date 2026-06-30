import { useMemo, useState } from 'react'
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
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { useQuery } from '@tanstack/react-query'

import { runResults, runStatus } from '@/api/agents'
import { TERMINAL_RUN_STATUSES } from '@/api/schemas/agents'
import { useLive } from '@/core/live'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { loadTrackedRuns } from '@/features/agents/runHistory'
import { RunStatusPill } from '@/features/agents/RunStatusPill'
import { TimeseriesChart } from '@/features/analytics/charts'
import { EventCombobox } from '@/features/analytics/EventCombobox'
import { densifyBuckets, rollingHourBuckets } from '@/features/analytics/timeseries'
import {
  COMMON_EVENTS,
  lastDays,
  todayUtcIso,
  utcDateRangeForLastHours,
} from '@/features/analytics/selectorModel'
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

type ThroughputPeriod = 'today' | 'week' | 'month'

const THROUGHPUT_PERIODS: Record<
  ThroughputPeriod,
  { label: string; days: number; interval: '1 HOUR' | '1 DAY'; granularity: 'hour' | 'day'; cadence: string }
> = {
  today: { label: 'in the last 24h', days: 1, interval: '1 HOUR', granularity: 'hour', cadence: 'hourly' },
  week: { label: 'this week', days: 7, interval: '1 DAY', granularity: 'day', cadence: 'daily' },
  month: { label: 'this month', days: 30, interval: '1 DAY', granularity: 'day', cadence: 'daily' },
}

function ThroughputCard() {
  const { projectId } = useWorkspace()
  const [chartEvent, setChartEvent] = useState('page')
  const [period, setPeriod] = useState<ThroughputPeriod>('today')
  const config = THROUGHPUT_PERIODS[period]
  const isToday = period === 'today'
  // rollingHourBuckets reads the current UTC hour internally, but the date-string
  // range deps don't change on a same-day hour rollover — so the rolling window
  // would lag by a slot until the next refetch. Tick once a minute and fold the
  // current UTC hour into the memo deps so it recomputes when the hour turns.
  const hourTick = Math.floor(useNow(60_000) / 3_600_000)
  // "today" is a rolling last-24h window in UTC — the pipeline buckets timestamps
  // in UTC, so the bins line up with the API's bucket strings (and with the UTC
  // clock shown in the header). Week/month stay calendar-date windows.
  const range = isToday ? utcDateRangeForLastHours(24) : lastDays(config.days)
  // The count API is date-granular and can't express "last 24h"; for today we
  // count the current UTC day. Week/month count their full date range.
  const countRange = isToday ? { start_date: todayUtcIso(), end_date: todayUtcIso() } : range
  const tsBody = projectId
    ? {
        project_id: projectId,
        ...range,
        selector: { event_name: chartEvent, filters: [] },
        interval: config.interval,
      }
    : null
  const tsQuery = useAnalyticsQuery('overview-throughput', tsBody, timeseriesEvents)
  const countBody = projectId
    ? {
        project_id: projectId,
        ...countRange,
        selectors: COMMON_EVENTS.map((event) => ({ event_name: event, filters: [] })),
      }
    : null
  const countQuery = useAnalyticsQuery('overview-counts', countBody, countEvents)

  const totalEvents = countQuery.data?.total_events ?? null
  const buckets = tsQuery.data?.buckets ?? []
  // The API returns only slots that had events; fill the gaps so the chart shows
  // every slot in the window (zeroed where there were none). "today" is a rolling
  // 24 UTC-hour window; week/month densify across their calendar dates.
  const filledBuckets = useMemo(
    () =>
      isToday
        ? rollingHourBuckets(buckets, 24)
        : densifyBuckets(buckets, range.start_date, range.end_date, config.granularity),
    // hourTick only matters for the isToday rolling window; harmless otherwise.
    [isToday, hourTick, buckets, range.start_date, range.end_date, config.granularity],
  )

  return (
    <Card className="lg:col-span-2">
      <CardHeader>
        <CardTitle>Event throughput</CardTitle>
        <CardDescription className="flex flex-wrap items-center gap-1.5">
          <span>{totalEvents !== null ? `${totalEvents.toLocaleString()} events` : 'Events'}</span>
          <Select
            value={period}
            onChange={(event) => setPeriod(event.target.value as ThroughputPeriod)}
            className="h-7 w-auto"
            aria-label="Time period"
          >
            <option value="today">today</option>
            <option value="week">this week</option>
            <option value="month">this month</option>
          </Select>
          <span>across known event names · {config.cadence}</span>
          <EventCombobox
            value={chartEvent}
            onChange={setChartEvent}
            ariaLabel="Chart event"
            className="w-44"
            triggerClassName="h-7"
          />
          <span>below</span>
        </CardDescription>
      </CardHeader>
      <CardContent>
        {tsQuery.isPending ? (
          <Skeleton className="h-48 w-full" />
        ) : tsQuery.error ? (
          <ErrorState error={tsQuery.error} onRetry={() => void tsQuery.refetch()} />
        ) : buckets.length === 0 ? (
          totalEvents !== null && totalEvents > 0 ? (
            <EmptyState
              title={`No ${chartEvent || 'matching'} events ${config.label}`}
              description="Other event types are coming in, but none of this type to chart yet."
            >
              <Link to="/analytics/events" className="text-sm font-medium underline underline-offset-4">
                Explore events →
              </Link>
            </EmptyState>
          ) : (
            <EmptyState title={`No events ${config.label}`} description="Is the SDK wired up?">
              <Link to="/settings/verify" className="text-sm font-medium underline underline-offset-4">
                Verify your integration →
              </Link>
            </EmptyState>
          )
        ) : (
          <>
            <p className="mb-2 text-xs text-muted-foreground">
              {isToday ? 'Rolling last 24 hours · UTC' : 'UTC'}
            </p>
            <TimeseriesChart buckets={filledBuckets} mode="bar" />
          </>
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

  const resultsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run-overview', latest?.run_id ?? 'none', 'results'],
    enabled: active !== null && latest !== null && (statusQuery.data?.insights_count ?? 0) > 0,
    staleTime: 60_000,
    queryFn: ({ signal }) =>
      runResults(serviceConnection(active!, 'agents'), latest!.run_id, { signal }),
  })
  const insightTitles = (resultsQuery.data?.insights ?? [])
    .slice(0, 3)
    .map((insight) => {
      const record = typeof insight === 'object' && insight !== null ? (insight as Record<string, unknown>) : {}
      const title = record.title ?? record.name ?? record.summary
      return typeof title === 'string' ? title : null
    })
    .filter((title): title is string => title !== null)

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
        {insightTitles.length > 0 ? (
          <div className="space-y-1">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Latest insights
            </p>
            <ul className="space-y-1 text-sm">
              {insightTitles.map((title) => (
                <li key={title} className="truncate" title={title}>
                  • {title}
                </li>
              ))}
            </ul>
          </div>
        ) : null}
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
