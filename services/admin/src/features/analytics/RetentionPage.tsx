// Retention (plan §5.5.4): classic triangle heatmap + average retention
// curve. Cell values arrive as 0–100 percentages.
import { Play } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { QUERY_PATHS, queryCurl, runRetention } from '@/api/query'
import type { RetentionRequest, RetentionResponse } from '@/api/types/query'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
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

interface RetentionView {
  range: DateRange
  cohortSelector: SelectorFormValues
  returnSelector: SelectorFormValues
  period: 'day' | 'week'
}

function heatColor(pct: number): string {
  return `rgba(16, 185, 129, ${Math.min(0.92, (pct / 100) * 0.92)})`
}

function RetentionGrid({ result, period }: { result: RetentionResponse; period: string }) {
  const cohorts = result.cohorts
  if (cohorts.length === 0) {
    return (
      <EmptyState
        title="No cohorts in range"
        description="No actors first matched the cohort event in the selected dates — names are exact-match."
      />
    )
  }
  const maxPeriods = Math.max(...cohorts.map((cohort) => cohort.retention.length))
  return (
    <div className="overflow-auto">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b text-left text-xs text-muted-foreground">
            <th className="py-1.5 pr-3 font-medium">Cohort</th>
            <th className="py-1.5 pr-3 text-right font-medium">Size</th>
            {Array.from({ length: maxPeriods }).map((_, index) => (
              <th key={index} className="px-1 py-1.5 text-center font-medium">
                {period} {index}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {cohorts.map((cohort) => (
            <tr key={cohort.cohort_date} className="border-b last:border-0">
              <td className="py-1 pr-3 font-mono text-xs">{cohort.cohort_date}</td>
              <td className="py-1 pr-3 text-right tabular-nums">{cohort.size.toLocaleString()}</td>
              {Array.from({ length: maxPeriods }).map((_, index) => {
                const pct = cohort.retention[index]
                if (pct === undefined) return <td key={index} />
                return (
                  <td key={index} className="p-0.5">
                    <div
                      className="rounded px-1 py-1 text-center text-xs tabular-nums"
                      style={{ backgroundColor: heatColor(pct) }}
                      title={`${pct}% · ~${Math.round((cohort.size * pct) / 100)} of ${cohort.size} users`}
                    >
                      {pct}%
                    </div>
                  </td>
                )
              })}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

function averageCurve(result: RetentionResponse): { label: string; value: number }[] {
  const maxPeriods = Math.max(0, ...result.cohorts.map((cohort) => cohort.retention.length))
  const points: { label: string; value: number }[] = []
  for (let index = 0; index < maxPeriods; index++) {
    const values = result.cohorts
      .map((cohort) => cohort.retention[index])
      .filter((value): value is number => value !== undefined)
    if (values.length === 0) continue
    points.push({
      label: String(index),
      value: Math.round((values.reduce((sum, value) => sum + value, 0) / values.length) * 100) / 100,
    })
  }
  return points
}

export function RetentionPage() {
  const { active, projectId } = useWorkspace()
  const [range, setRange] = useState<DateRange>(lastDays(30))
  const [cohortSelector, setCohortSelector] = useState<SelectorFormValues>(emptySelector('page'))
  const [returnSelector, setReturnSelector] = useState<SelectorFormValues>(emptySelector('page'))
  const [period, setPeriod] = useState<'day' | 'week'>('day')
  const [body, setBody] = useState<RetentionRequest | null>(null)
  const retentionQuery = useAnalyticsQuery('retention', body, runRetention)
  const conn = active ? serviceConnection(active, 'query') : null

  const run = () => {
    if (!projectId) return
    const cohortProblem = selectorProblem(cohortSelector)
    if (cohortProblem) {
      toast.error(`Cohort: ${cohortProblem}`)
      return
    }
    const returnProblem = selectorProblem(returnSelector)
    if (returnProblem) {
      toast.error(`Return: ${returnProblem}`)
      return
    }
    setBody({
      project_id: projectId,
      ...range,
      cohort_selector: selectorToWire(cohortSelector),
      return_selector: selectorToWire(returnSelector),
      cohort_mode: 'first_match_in_window',
      period,
    })
  }

  const view: RetentionView = { range, cohortSelector, returnSelector, period }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Window-relative retention"
        description="Actors enter on their first matching cohort event in the selected dates. Existing actors may re-enter on that first in-window match; this is not lifetime acquisition retention."
        actions={
          <SavedViews
            screen="retention"
            current={view}
            onLoad={(loaded) => {
              setRange(loaded.range)
              setCohortSelector(loaded.cohortSelector)
              setReturnSelector(loaded.returnSelector)
              setPeriod(loaded.period)
            }}
          />
        }
      />
      <DateRangePicker value={range} onChange={setRange} />

      <div className="grid gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">First matching event in selected dates</CardTitle>
          </CardHeader>
          <CardContent>
            <SelectorBuilder value={cohortSelector} onChange={setCohortSelector} eventLabel="Window-entry event name" />
          </CardContent>
        </Card>
        <Card>
          <CardHeader className="pb-2">
            <CardTitle className="text-sm">…came back and did</CardTitle>
          </CardHeader>
          <CardContent>
            <SelectorBuilder value={returnSelector} onChange={setReturnSelector} eventLabel="Return event name" />
          </CardContent>
        </Card>
      </div>

      <div className="flex items-end gap-3">
        <div className="space-y-1.5">
          <Label>Period</Label>
          <Select
            value={period}
            onChange={(event) => setPeriod(event.target.value as 'day' | 'week')}
            className="w-32"
          >
            <option value="day">day</option>
            <option value="week">week</option>
          </Select>
        </div>
        <Button size="sm" onClick={run}>
          <Play />
          Run retention
        </Button>
      </div>

      {retentionQuery.isLoading ? <Skeleton className="h-64 w-full" /> : null}
      {retentionQuery.error ? (
        <ErrorState error={retentionQuery.error} onRetry={() => void retentionQuery.refetch()} />
      ) : null}
      {retentionQuery.data ? (
        <div className="space-y-4">
          <div className="flex justify-end">
            <ResultActions
              curl={conn && body ? queryCurl(conn, QUERY_PATHS.retention, body) : null}
              raw={retentionQuery.data}
              csv={{
                filename: 'retention',
                headers: [
                  'cohort_date',
                  'size',
                  ...Array.from(
                    { length: Math.max(0, ...retentionQuery.data.cohorts.map((cohort) => cohort.retention.length)) },
                    (_, index) => `${body?.period ?? 'period'}_${index}`,
                  ),
                ],
                rows: retentionQuery.data.cohorts.map((cohort) => [
                  cohort.cohort_date,
                  cohort.size,
                  ...cohort.retention,
                ]),
              }}
            />
          </div>
          <Card>
            <CardContent className="p-4">
              <RetentionGrid result={retentionQuery.data} period={body?.period ?? 'day'} />
            </CardContent>
          </Card>
          {retentionQuery.data.cohorts.length > 0 ? (
            <Card>
              <CardHeader className="pb-2">
                <CardTitle className="text-sm">Average retention across cohorts</CardTitle>
              </CardHeader>
              <CardContent>
                <SimpleLineChart data={averageCurve(retentionQuery.data)} unit="%" />
              </CardContent>
            </Card>
          ) : null}
        </div>
      ) : null}
    </div>
  )
}
