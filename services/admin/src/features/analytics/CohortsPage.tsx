// Cohort comparison (plan §5.5.5): a metric split by a user-property value —
// small-multiple timeseries plus a summary table.
import { Play } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { QUERY_PATHS, queryCurl, runCohort } from '@/api/query'
import { PROPERTY_NAME_PATTERN } from '@/api/schemas/query'
import type { CohortRequest } from '@/api/types/query'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { serviceConnection, useWorkspace } from '@/core/workspace'

import { SimpleLineChart } from './charts'
import { DateRangePicker } from './DateRangePicker'
import { ResultActions } from './ResultActions'
import { SavedViews } from './SavedViews'
import { SelectorBuilder } from './SelectorBuilder'
import {
  emptySelector,
  lastDays,
  selectorProblem,
  selectorToWire,
  type DateRange,
  type SelectorFormValues,
} from './selectorModel'
import { useAnalyticsQuery } from './useAnalyticsQuery'

const HIGH_CARDINALITY = 12

interface CohortsView {
  range: DateRange
  cohortProperty: string
  metricSelector: SelectorFormValues
}

export function CohortsPage() {
  const { active, projectId } = useWorkspace()
  const [range, setRange] = useState<DateRange>(lastDays(30))
  const [cohortProperty, setCohortProperty] = useState('plan')
  const [metricSelector, setMetricSelector] = useState<SelectorFormValues>(emptySelector('page'))
  const [body, setBody] = useState<CohortRequest | null>(null)
  const cohortQuery = useAnalyticsQuery('cohorts', body, runCohort)
  const conn = active ? serviceConnection(active, 'query') : null

  const run = () => {
    if (!projectId) return
    if (!PROPERTY_NAME_PATTERN.test(cohortProperty.trim())) {
      toast.error('Cohort property must match the property-name pattern')
      return
    }
    const problem = selectorProblem(metricSelector)
    if (problem) {
      toast.error(problem)
      return
    }
    setBody({
      project_id: projectId,
      ...range,
      cohort_property: cohortProperty.trim(),
      metric_selector: selectorToWire(metricSelector),
    })
  }

  const view: CohortsView = { range, cohortProperty, metricSelector }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Cohort comparison"
        description="Compare a metric across user segments defined by a property value."
        actions={
          <SavedViews
            screen="cohorts"
            current={view}
            onLoad={(loaded) => {
              setRange(loaded.range)
              setCohortProperty(loaded.cohortProperty)
              setMetricSelector(loaded.metricSelector)
            }}
          />
        }
      />
      <DateRangePicker value={range} onChange={setRange} />

      <Card>
        <CardContent className="space-y-4 p-4">
          <div className="space-y-1.5">
            <Label>Cohort property</Label>
            <Input
              value={cohortProperty}
              onChange={(event) => setCohortProperty(event.target.value)}
              placeholder="plan"
              className="w-48 font-mono text-xs"
            />
          </div>
          <div className="space-y-1.5">
            <Label>Metric event</Label>
            <SelectorBuilder value={metricSelector} onChange={setMetricSelector} eventLabel="Metric event name" />
          </div>
          <Button size="sm" onClick={run}>
            <Play />
            Compare cohorts
          </Button>
        </CardContent>
      </Card>

      {cohortQuery.isLoading ? <Skeleton className="h-64 w-full" /> : null}
      {cohortQuery.error ? (
        <ErrorState error={cohortQuery.error} onRetry={() => void cohortQuery.refetch()} />
      ) : null}
      {cohortQuery.data ? (
        <div className="space-y-4">
          <div className="flex items-center justify-between gap-3">
            <p className="text-sm text-muted-foreground">
              <code className="font-mono text-xs">{cohortQuery.data.metric_selector}</code> by{' '}
              <code className="font-mono text-xs">{cohortQuery.data.cohort_property}</code>
            </p>
            <ResultActions
              curl={conn && body ? queryCurl(conn, QUERY_PATHS.cohort, body) : null}
              raw={cohortQuery.data}
              csv={{
                filename: 'cohorts',
                headers: ['cohort_value', 'total_events', 'total_users'],
                rows: cohortQuery.data.cohorts.map((cohort) => [
                  cohort.cohort_value,
                  cohort.total_events,
                  cohort.total_users,
                ]),
              }}
            />
          </div>

          {cohortQuery.data.cohorts.length === 0 ? (
            <Card>
              <CardContent className="p-4">
                <EmptyState
                  title="No cohorts found"
                  description="No matching events carried this property in the date range."
                />
              </CardContent>
            </Card>
          ) : (
            <>
              {cohortQuery.data.cohorts.length > HIGH_CARDINALITY ? (
                <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                  This property has {cohortQuery.data.cohorts.length} distinct values — consider a
                  lower-cardinality property for a readable comparison.
                </p>
              ) : null}
              <Card>
                <CardContent className="p-4">
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Cohort value</TableHead>
                        <TableHead className="text-right">Events</TableHead>
                        <TableHead className="text-right">Users</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {cohortQuery.data.cohorts.map((cohort) => (
                        <TableRow key={cohort.cohort_value}>
                          <TableCell className="font-mono text-xs">
                            {cohort.cohort_value || '(empty)'}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {cohort.total_events.toLocaleString()}
                          </TableCell>
                          <TableCell className="text-right tabular-nums">
                            {cohort.total_users.toLocaleString()}
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                </CardContent>
              </Card>
              <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-3">
                {cohortQuery.data.cohorts.slice(0, HIGH_CARDINALITY).map((cohort) => (
                  <Card key={cohort.cohort_value}>
                    <CardHeader className="pb-2">
                      <CardTitle className="font-mono text-xs">
                        {cohort.cohort_value || '(empty)'}
                      </CardTitle>
                    </CardHeader>
                    <CardContent>
                      <SimpleLineChart
                        height={140}
                        data={cohort.timeseries.map((point) => ({
                          label: point.day?.slice(5) ?? '',
                          value: point.event_count,
                        }))}
                      />
                    </CardContent>
                  </Card>
                ))}
              </div>
            </>
          )}
        </div>
      ) : null}
    </div>
  )
}
