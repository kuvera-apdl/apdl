// Funnel builder (plan §5.5.3): ordered steps, window_days, drop-off
// highlighting. Conversion rates arrive as 0–100 percentages.
import { ArrowDown, ArrowUp, Play, Plus, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { toast } from 'sonner'

import { QUERY_PATHS, queryCurl, runFunnel } from '@/api/query'
import type { FunnelRequest, FunnelResponse } from '@/api/types/query'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { StatCard } from '@/components/shared/StatCard'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { formatPercent } from '@/lib/format'

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

interface FunnelView {
  range: DateRange
  steps: SelectorFormValues[]
  windowDays: number
}

function FunnelChart({ result }: { result: FunnelResponse }) {
  const steps = result.steps
  if (steps.length === 0) {
    return (
      <EmptyState
        title="No funnel data"
        description="Check event name spelling — names are exact-match — and widen the date range."
      />
    )
  }
  // Largest drop-off edge (steps after the first).
  let worstIndex = -1
  let worstDrop = 0
  steps.forEach((step, index) => {
    if (index === 0) return
    const drop = 100 - step.conversion_rate
    if (drop > worstDrop) {
      worstDrop = drop
      worstIndex = index
    }
  })

  return (
    <div className="space-y-1.5">
      {steps.map((step, index) => (
        <div key={step.step}>
          {index > 0 ? (
            <p
              className={
                index === worstIndex && worstDrop > 0
                  ? 'py-1 text-xs font-medium text-destructive'
                  : 'py-1 text-xs text-muted-foreground'
              }
            >
              −{formatPercent(100 - step.conversion_rate)} between step {index} and {index + 1}
              {index === worstIndex && worstDrop > 0 ? ' · biggest drop-off' : ''}
            </p>
          ) : null}
          <div className="space-y-1">
            <div className="flex items-baseline justify-between gap-2 text-sm">
              <span>
                <span className="mr-2 inline-flex h-5 w-5 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
                  {step.step}
                </span>
                <code className="font-mono text-xs">{step.selector}</code>
              </span>
              <span className="shrink-0 tabular-nums text-muted-foreground">
                {step.count.toLocaleString()} users · step {formatPercent(step.conversion_rate)} · overall{' '}
                {formatPercent(step.overall_rate)}
              </span>
            </div>
            <div className="h-5 w-full overflow-hidden rounded bg-muted">
              <div className="h-full rounded bg-sky-500" style={{ width: `${step.overall_rate}%` }} />
            </div>
          </div>
        </div>
      ))}
    </div>
  )
}

export function FunnelsPage() {
  const { active, projectId } = useWorkspace()
  const [range, setRange] = useState<DateRange>(lastDays(7))
  const [steps, setSteps] = useState<SelectorFormValues[]>([
    emptySelector('page'),
    emptySelector('$click'),
  ])
  const [windowDays, setWindowDays] = useState(7)
  const [body, setBody] = useState<FunnelRequest | null>(null)
  const funnelQuery = useAnalyticsQuery('funnel', body, runFunnel)
  const conn = active ? serviceConnection(active, 'query') : null

  const moveStep = (from: number, to: number) => {
    setSteps((previous) => {
      const next = [...previous]
      const [moved] = next.splice(from, 1)
      next.splice(to, 0, moved!)
      return next
    })
  }

  const run = () => {
    if (!projectId) return
    for (const [index, step] of steps.entries()) {
      const problem = selectorProblem(step)
      if (problem) {
        toast.error(`Step ${index + 1}: ${problem}`)
        return
      }
    }
    setBody({ project_id: projectId, ...range, steps: steps.map(selectorToWire), window_days: windowDays })
  }

  const view: FunnelView = { range, steps, windowDays }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Funnels"
        description="Ordered conversion through 2–20 steps within a completion window."
        actions={
          <SavedViews
            screen="funnels"
            current={view}
            onLoad={(loaded) => {
              setRange(loaded.range)
              setSteps(loaded.steps)
              setWindowDays(loaded.windowDays)
            }}
          />
        }
      />
      <DateRangePicker value={range} onChange={setRange} />

      <Card>
        <CardContent className="space-y-4 p-4">
          {steps.map((step, index) => (
            <div key={index} className="rounded-md border p-3">
              <div className="mb-2 flex items-center gap-1">
                <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
                  {index + 1}
                </span>
                <span className="ml-auto flex items-center">
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={index === 0}
                    onClick={() => moveStep(index, index - 1)}
                    aria-label={`Move step ${index + 1} up`}
                  >
                    <ArrowUp />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={index === steps.length - 1}
                    onClick={() => moveStep(index, index + 1)}
                    aria-label={`Move step ${index + 1} down`}
                  >
                    <ArrowDown />
                  </Button>
                  <Button
                    variant="ghost"
                    size="icon"
                    disabled={steps.length <= 2}
                    onClick={() => setSteps((previous) => previous.filter((_, stepIndex) => stepIndex !== index))}
                    aria-label={`Remove step ${index + 1}`}
                  >
                    <Trash2 />
                  </Button>
                </span>
              </div>
              <SelectorBuilder
                value={step}
                onChange={(next) =>
                  setSteps((previous) => previous.map((entry, entryIndex) => (entryIndex === index ? next : entry)))
                }
                eventLabel={`Step ${index + 1} event name`}
              />
            </div>
          ))}
          <div className="flex flex-wrap items-end gap-3">
            <Button
              variant="outline"
              size="sm"
              onClick={() => setSteps((previous) => [...previous, emptySelector()])}
              disabled={steps.length >= 20}
            >
              <Plus />
              Add step
            </Button>
            <div className="space-y-1.5">
              <Label>Window (days)</Label>
              <Input
                type="number"
                min={1}
                max={90}
                value={windowDays}
                onChange={(event) => setWindowDays(Math.min(90, Math.max(1, Number(event.target.value) || 7)))}
                className="w-24 tabular-nums"
              />
            </div>
            <Button size="sm" onClick={run}>
              <Play />
              Run funnel
            </Button>
          </div>
        </CardContent>
      </Card>

      {funnelQuery.isLoading ? <Skeleton className="h-64 w-full" /> : null}
      {funnelQuery.error ? (
        <ErrorState error={funnelQuery.error} onRetry={() => void funnelQuery.refetch()} />
      ) : null}
      {funnelQuery.data ? (
        <div className="space-y-4">
          <div className="flex items-start justify-between gap-4">
            <div className="w-56">
              <StatCard
                label="Overall conversion"
                value={formatPercent(funnelQuery.data.overall_conversion)}
                hint={`${funnelQuery.data.steps.length} steps · ${body?.window_days ?? 0}-day window`}
              />
            </div>
            <ResultActions
              curl={conn && body ? queryCurl(conn, QUERY_PATHS.funnel, body) : null}
              raw={funnelQuery.data}
              csv={{
                filename: 'funnel',
                headers: ['step', 'selector', 'count', 'conversion_rate', 'overall_rate'],
                rows: funnelQuery.data.steps.map((step) => [
                  step.step,
                  step.selector,
                  step.count,
                  step.conversion_rate,
                  step.overall_rate,
                ]),
              }}
            />
          </div>
          <Card>
            <CardContent className="p-4">
              <FunnelChart result={funnelQuery.data} />
            </CardContent>
          </Card>
        </div>
      ) : null}
    </div>
  )
}
