// Events explorer (plan §5.5.2): three modes sharing one date range. Queries
// run on submit, not on every keystroke.
import { Play, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { breakdownEvents, countEvents, QUERY_PATHS, queryCurl, timeseriesEvents } from '@/api/query'
import type {
  BreakdownRequest,
  EventCountRequest,
  TimeInterval,
  TimeseriesRequest,
} from '@/api/types/query'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { formatPercent } from '@/lib/format'

import { TimeseriesChart } from './charts'
import { DateRangePicker } from './DateRangePicker'
import { ResultActions } from './ResultActions'
import { SavedViews } from './SavedViews'
import { SelectorBuilder } from './SelectorBuilder'
import {
  emptySelector,
  lastDays,
  selectorProblem,
  selectorSummary,
  selectorToWire,
  type DateRange,
  type SelectorFormValues,
} from './selectorModel'
import { useAnalyticsQuery } from './useAnalyticsQuery'

const MODES = ['counts', 'timeseries', 'breakdown'] as const
type Mode = (typeof MODES)[number]

interface EventsView {
  range: DateRange
  countSelectors: SelectorFormValues[]
  tsSelector: SelectorFormValues
  interval: TimeInterval
  bdSelector: SelectorFormValues
  property: string
  limit: number
}

const ZERO_DATA_HINT =
  'No events matched. Event names are exact-match — check spelling, the date range, and that your integration is sending events.'

export function EventsExplorerPage() {
  const { active, projectId } = useWorkspace()
  const [searchParams, setSearchParams] = useSearchParams()
  const mode: Mode = MODES.includes(searchParams.get('mode') as Mode)
    ? (searchParams.get('mode') as Mode)
    : 'counts'

  const [range, setRange] = useState<DateRange>(lastDays(7))
  const [countSelectors, setCountSelectors] = useState<SelectorFormValues[]>([emptySelector('page')])
  const [tsSelector, setTsSelector] = useState<SelectorFormValues>(emptySelector('page'))
  const [interval, setIntervalValue] = useState<TimeInterval>('1 DAY')
  const [bdSelector, setBdSelector] = useState<SelectorFormValues>(emptySelector('$click'))
  const [property, setProperty] = useState('')
  const [limit, setLimit] = useState(20)

  const [countBody, setCountBody] = useState<EventCountRequest | null>(null)
  const [tsBody, setTsBody] = useState<TimeseriesRequest | null>(null)
  const [bdBody, setBdBody] = useState<BreakdownRequest | null>(null)

  const countQuery = useAnalyticsQuery('events-count', countBody, countEvents)
  const tsQuery = useAnalyticsQuery('events-timeseries', tsBody, timeseriesEvents)
  const bdQuery = useAnalyticsQuery('events-breakdown', bdBody, breakdownEvents)
  const [chartMode, setChartMode] = useState<'line' | 'bar'>('line')

  const conn = active ? serviceConnection(active, 'query') : null

  const runCounts = () => {
    if (!projectId) return
    for (const selector of countSelectors) {
      const problem = selectorProblem(selector)
      if (problem) {
        toast.error(`${selectorSummary(selector)}: ${problem}`)
        return
      }
    }
    setCountBody({ project_id: projectId, ...range, selectors: countSelectors.map(selectorToWire) })
  }

  const runTimeseries = () => {
    if (!projectId) return
    const problem = selectorProblem(tsSelector)
    if (problem) {
      toast.error(problem)
      return
    }
    setTsBody({ project_id: projectId, ...range, selector: selectorToWire(tsSelector), interval })
  }

  const runBreakdown = () => {
    if (!projectId) return
    const problem = selectorProblem(bdSelector)
    if (problem) {
      toast.error(problem)
      return
    }
    if (!property.trim()) {
      toast.error('Breakdown property is required')
      return
    }
    setBdBody({
      project_id: projectId,
      ...range,
      selector: selectorToWire(bdSelector),
      property: property.trim(),
      limit,
    })
  }

  const view: EventsView = { range, countSelectors, tsSelector, interval, bdSelector, property, limit }
  const loadView = (loaded: EventsView) => {
    setRange(loaded.range)
    setCountSelectors(loaded.countSelectors)
    setTsSelector(loaded.tsSelector)
    setIntervalValue(loaded.interval)
    setBdSelector(loaded.bdSelector)
    setProperty(loaded.property)
    setLimit(loaded.limit)
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Events explorer"
        description="Counts, timeseries, and property breakdowns over raw events."
        actions={<SavedViews screen="events" current={view} onLoad={loadView} />}
      />
      <DateRangePicker value={range} onChange={setRange} />

      <Tabs
        value={mode}
        onValueChange={(value) =>
          setSearchParams(
            (previous) => {
              const next = new URLSearchParams(previous)
              if (value === 'counts') next.delete('mode')
              else next.set('mode', value)
              return next
            },
            { replace: true },
          )
        }
      >
        <TabsList>
          <TabsTrigger value="counts">Counts</TabsTrigger>
          <TabsTrigger value="timeseries">Timeseries</TabsTrigger>
          <TabsTrigger value="breakdown">Breakdown</TabsTrigger>
        </TabsList>

        <TabsContent value="counts" className="space-y-4">
          <Card>
            <CardContent className="space-y-4 p-4">
              {countSelectors.map((selector, index) => (
                <div key={index} className="flex items-start gap-2">
                  <div className="flex-1">
                    <SelectorBuilder
                      value={selector}
                      onChange={(next) =>
                        setCountSelectors((previous) =>
                          previous.map((entry, entryIndex) => (entryIndex === index ? next : entry)),
                        )
                      }
                      eventLabel={`Selector ${index + 1} event name`}
                    />
                  </div>
                  <Button
                    variant="ghost"
                    size="icon"
                    onClick={() =>
                      setCountSelectors((previous) => previous.filter((_, entryIndex) => entryIndex !== index))
                    }
                    disabled={countSelectors.length <= 1}
                    aria-label={`Remove selector ${index + 1}`}
                  >
                    <Trash2 />
                  </Button>
                </div>
              ))}
              <div className="flex items-center gap-2">
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => setCountSelectors((previous) => [...previous, emptySelector()])}
                  disabled={countSelectors.length >= 20}
                >
                  <Plus />
                  Add selector
                </Button>
                <Button size="sm" onClick={runCounts}>
                  <Play />
                  Run
                </Button>
              </div>
            </CardContent>
          </Card>

          {countQuery.isLoading ? <Skeleton className="h-40 w-full" /> : null}
          {countQuery.error ? (
            <ErrorState error={countQuery.error} onRetry={() => void countQuery.refetch()} />
          ) : null}
          {countQuery.data ? (
            <Card>
              <CardContent className="space-y-3 p-4">
                <div className="flex items-center justify-between">
                  <p className="text-sm text-muted-foreground">
                    {countQuery.data.total_events.toLocaleString()} events ·{' '}
                    {countQuery.data.total_users.toLocaleString()} users (sum of uniques)
                  </p>
                  <ResultActions
                    curl={conn && countBody ? queryCurl(conn, QUERY_PATHS.count, countBody) : null}
                    raw={countQuery.data}
                    csv={{
                      filename: 'event-counts',
                      headers: ['selector', 'event_count', 'unique_users'],
                      rows: countQuery.data.results.map((row) => [
                        row.selector,
                        row.event_count,
                        row.unique_users,
                      ]),
                    }}
                  />
                </div>
                {countQuery.data.results.length === 0 || countQuery.data.total_events === 0 ? (
                  <EmptyState title="No matching events" description={ZERO_DATA_HINT} />
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Selector</TableHead>
                        <TableHead className="text-right">Events</TableHead>
                        <TableHead className="text-right">Unique users</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {countQuery.data.results.map((row, index) => (
                        <TableRow key={index}>
                          <TableCell className="font-mono text-xs">{row.selector}</TableCell>
                          <TableCell className="text-right tabular-nums">
                            {row.event_count.toLocaleString()}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {row.unique_users.toLocaleString()}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          ) : null}
        </TabsContent>

        <TabsContent value="timeseries" className="space-y-4">
          <Card>
            <CardContent className="space-y-4 p-4">
              <SelectorBuilder value={tsSelector} onChange={setTsSelector} />
              <div className="flex flex-wrap items-end gap-3">
                <div className="space-y-1.5">
                  <Label>Interval</Label>
                  <Select
                    value={interval}
                    onChange={(event) => setIntervalValue(event.target.value as TimeInterval)}
                    className="w-36"
                  >
                    <option value="1 HOUR">1 HOUR</option>
                    <option value="1 DAY">1 DAY</option>
                    <option value="1 WEEK">1 WEEK</option>
                    <option value="1 MONTH">1 MONTH</option>
                  </Select>
                </div>
                <Button size="sm" onClick={runTimeseries}>
                  <Play />
                  Run
                </Button>
              </div>
            </CardContent>
          </Card>

          {tsQuery.isLoading ? <Skeleton className="h-72 w-full" /> : null}
          {tsQuery.error ? <ErrorState error={tsQuery.error} onRetry={() => void tsQuery.refetch()} /> : null}
          {tsQuery.data ? (
            <Card>
              <CardContent className="space-y-3 p-4">
                <div className="flex items-center justify-between">
                  <div className="flex items-center gap-2">
                    <p className="font-mono text-xs text-muted-foreground">{tsQuery.data.selector}</p>
                    <Button
                      variant="outline"
                      size="sm"
                      onClick={() => setChartMode((previous) => (previous === 'line' ? 'bar' : 'line'))}
                    >
                      {chartMode === 'line' ? 'Bar' : 'Line'}
                    </Button>
                  </div>
                  <ResultActions
                    curl={conn && tsBody ? queryCurl(conn, QUERY_PATHS.timeseries, tsBody) : null}
                    raw={tsQuery.data}
                    csv={{
                      filename: 'event-timeseries',
                      headers: ['bucket', 'event_count', 'unique_users'],
                      rows: tsQuery.data.buckets.map((bucket) => [
                        bucket.bucket,
                        bucket.event_count,
                        bucket.unique_users,
                      ]),
                    }}
                  />
                </div>
                {tsQuery.data.buckets.length === 0 ? (
                  <EmptyState title="No matching events" description={ZERO_DATA_HINT} />
                ) : (
                  <TimeseriesChart buckets={tsQuery.data.buckets} mode={chartMode} />
                )}
              </CardContent>
            </Card>
          ) : null}
        </TabsContent>

        <TabsContent value="breakdown" className="space-y-4">
          <Card>
            <CardContent className="space-y-4 p-4">
              <SelectorBuilder value={bdSelector} onChange={setBdSelector} />
              <div className="flex flex-wrap items-end gap-3">
                <div className="space-y-1.5">
                  <Label>Property</Label>
                  <Input
                    value={property}
                    onChange={(event) => setProperty(event.target.value)}
                    placeholder="href"
                    className="w-44 font-mono text-xs"
                  />
                </div>
                <div className="space-y-1.5">
                  <Label>Limit</Label>
                  <Input
                    type="number"
                    min={1}
                    max={100}
                    value={limit}
                    onChange={(event) => setLimit(Math.min(100, Math.max(1, Number(event.target.value) || 20)))}
                    className="w-24 tabular-nums"
                  />
                </div>
                <Button size="sm" onClick={runBreakdown}>
                  <Play />
                  Run
                </Button>
              </div>
            </CardContent>
          </Card>

          {bdQuery.isLoading ? <Skeleton className="h-40 w-full" /> : null}
          {bdQuery.error ? <ErrorState error={bdQuery.error} onRetry={() => void bdQuery.refetch()} /> : null}
          {bdQuery.data ? (
            <Card>
              <CardContent className="space-y-3 p-4">
                <div className="flex items-center justify-between">
                  <p className="text-sm text-muted-foreground">
                    Top values of <code className="font-mono text-xs">{bdQuery.data.property}</code>
                  </p>
                  <ResultActions
                    curl={conn && bdBody ? queryCurl(conn, QUERY_PATHS.breakdown, bdBody) : null}
                    raw={bdQuery.data}
                    csv={{
                      filename: 'event-breakdown',
                      headers: ['property_type', 'property_value', 'event_count', 'unique_users'],
                      rows: bdQuery.data.results.map((row) => [
                        row.property_type,
                        row.property_value,
                        row.event_count,
                        row.unique_users,
                      ]),
                    }}
                  />
                </div>
                {bdQuery.data.results.length === 0 ? (
                  <EmptyState title="No matching events" description={ZERO_DATA_HINT} />
                ) : (
                  <>
                    {(() => {
                      const max = Math.max(...bdQuery.data.results.map((row) => row.event_count), 1)
                      return (
                        <div className="space-y-2">
                          {bdQuery.data.results.map((row) => (
                            <div
                              key={`${row.property_type}:${row.property_value}`}
                              className="space-y-1"
                            >
                              <div className="flex items-baseline justify-between gap-2 text-sm">
                                <div className="flex min-w-0 items-center gap-2">
                                  <code className="truncate font-mono text-xs">
                                    {row.property_value || '(empty)'}
                                  </code>
                                  <Badge variant="outline" className="shrink-0 font-mono font-normal">
                                    {row.property_type}
                                  </Badge>
                                </div>
                                <span className="shrink-0 tabular-nums text-muted-foreground">
                                  {row.event_count.toLocaleString()} ·{' '}
                                  {formatPercent((row.event_count / max) * 100)} of top
                                </span>
                              </div>
                              <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
                                <div
                                  className="h-full bg-sky-500"
                                  style={{ width: `${(row.event_count / max) * 100}%` }}
                                />
                              </div>
                            </div>
                          ))}
                        </div>
                      )
                    })()}
                    {bdQuery.data.results.length === bdBody?.limit ? (
                      <p className="text-xs text-muted-foreground">
                        Showing the top {bdBody.limit} values — other values are not shown.
                      </p>
                    ) : null}
                  </>
                )}
              </CardContent>
            </Card>
          ) : null}
        </TabsContent>
      </Tabs>
    </div>
  )
}
