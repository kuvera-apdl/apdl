// Experiment results (plan §5.4.3): an experiment is measured through a
// flag's exposures. The flag link is now a first-class field (gap G5), so the
// flag picker defaults to the experiment's flag_key; metric is still chosen at
// query time. Stats queries are heavy: manual run + explicit refresh.
import { Play, RefreshCw } from 'lucide-react'
import { useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { experimentResultsCurl } from '@/api/experiments'
import { ApiError } from '@/api/http'
import { countEvents, timeseriesEvents } from '@/api/query'
import type { AnalysisMethod, ExperimentResult } from '@/api/types/experiments'
import { CurlButton } from '@/components/shared/CurlButton'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { SimpleLineChart } from '@/features/analytics/charts'
import { lastDays } from '@/features/analytics/selectorModel'
import { useAnalyticsQuery } from '@/features/analytics/useAnalyticsQuery'
import { useExperimentResultsQuery } from '@/features/experiments/hooks'
import { useFlagsQuery } from '@/features/flags/hooks'
import { cn } from '@/lib/utils'

interface ResultInputs {
  flagKey: string
  metric: string
  method: AnalysisMethod
}

function storageKey(wsId: string, experimentKey: string): string {
  return `apdl-admin:exp-results:${wsId}:${experimentKey}`
}

function loadInputs(wsId: string, experimentKey: string, defaultFlagKey: string): ResultInputs {
  try {
    const raw = localStorage.getItem(storageKey(wsId, experimentKey))
    if (raw) {
      const stored = JSON.parse(raw) as ResultInputs
      // Prefer a previously chosen flag, but fall back to the experiment's link.
      return { ...stored, flagKey: stored.flagKey || defaultFlagKey }
    }
  } catch {
    // fall through to defaults
  }
  return { flagKey: defaultFlagKey, metric: '', method: 'sequential' }
}

function ConfidenceIntervalBar({ interval }: { interval: [number, number] }) {
  const [low, high] = interval
  const domain = Math.max(Math.abs(low), Math.abs(high), 0.0001) * 1.25
  const toPct = (value: number) => ((value + domain) / (2 * domain)) * 100
  const crossesZero = low <= 0 && high >= 0
  return (
    <div className="space-y-1">
      <div className="relative h-3 w-full rounded-full bg-muted">
        <div className="absolute inset-y-0 w-px bg-foreground/50" style={{ left: '50%' }} title="zero" />
        <div
          className={cn('absolute inset-y-0.5 rounded-full', crossesZero ? 'bg-amber-500/70' : 'bg-emerald-500/80')}
          style={{ left: `${toPct(low)}%`, width: `${Math.max(1, toPct(high) - toPct(low))}%` }}
        />
      </div>
      <p className="text-xs tabular-nums text-muted-foreground">
        95% CI [{low.toFixed(4)}, {high.toFixed(4)}]{crossesZero ? ' — crosses zero' : ''}
      </p>
    </div>
  )
}

function MethodBlock({ result }: { result: ExperimentResult }) {
  if (result.method === 'bayesian') {
    return (
      <div className="space-y-1 text-sm">
        <p className="text-muted-foreground">
          Beta(1,1) priors, 100k Monte-Carlo simulations; the metric is treated as binary
          conversion (any value &gt; 0). Probability details are in the recommendation above.
        </p>
      </div>
    )
  }
  return (
    <div className="space-y-3 text-sm">
      {result.effect_size !== null ? (
        <p>
          Effect size (Cohen's d):{' '}
          <span className="font-medium tabular-nums">{result.effect_size.toFixed(4)}</span>
        </p>
      ) : null}
      {result.confidence_interval ? <ConfidenceIntervalBar interval={result.confidence_interval} /> : null}
      {result.p_value !== null ? (
        <p className="tabular-nums">
          {result.method === 'sequential' ? 'Always-valid p-value' : 'p-value'}:{' '}
          <span className={cn('font-medium', result.p_value < 0.05 ? 'text-emerald-600' : '')}>
            {result.p_value.toExponential(3)}
          </span>{' '}
          <span className="text-muted-foreground">(α = 0.05)</span>
        </p>
      ) : null}
      {result.method === 'sequential' ? (
        <p className="text-xs text-muted-foreground">
          Safe to peek — mSPRT (τ=1e-4) controls the error rate under continuous monitoring.
        </p>
      ) : null}
    </div>
  )
}

export function ExperimentResultsTab({
  experimentKey,
  defaultFlagKey = '',
}: {
  experimentKey: string
  defaultFlagKey?: string
}) {
  const { active, projectId } = useWorkspace()
  const wsId = active?.id ?? 'none'
  const flagsQuery = useFlagsQuery()
  const [inputs, setInputs] = useState<ResultInputs>(() =>
    loadInputs(wsId, experimentKey, defaultFlagKey),
  )
  const [params, setParams] = useState<
    (ResultInputs & { projectId: string }) | null
  >(null)

  useEffect(() => {
    localStorage.setItem(storageKey(wsId, experimentKey), JSON.stringify(inputs))
  }, [wsId, experimentKey, inputs])

  const resultsQuery = useExperimentResultsQuery(experimentKey, params)

  // Sanity context: unique users exposed to the flag (independent of stats).
  const exposureBody = useMemo(
    () =>
      params
        ? {
            project_id: params.projectId,
            ...lastDays(90),
            selectors: [
              {
                event_name: '$feature_flag_exposure',
                filters: [{ property: 'flag_key', operator: 'eq' as const, value: params.flagKey }],
              },
            ],
          }
        : null,
    [params],
  )
  const exposureQuery = useAnalyticsQuery('exp-exposures', exposureBody, countEvents)

  const errorBody = useMemo(
    () =>
      params
        ? {
            project_id: params.projectId,
            ...lastDays(7),
            selector: { event_name: '$frontend_error', filters: [] },
            interval: '1 DAY' as const,
          }
        : null,
    [params],
  )
  const errorQuery = useAnalyticsQuery('exp-errors', errorBody, timeseriesEvents)

  const run = () => {
    if (!projectId) return
    if (!inputs.flagKey) {
      toast.error('Pick the flag whose exposures measure this experiment')
      return
    }
    if (!inputs.metric.trim()) {
      toast.error('Enter the metric event name')
      return
    }
    setParams({ ...inputs, metric: inputs.metric.trim(), projectId })
  }

  const result = resultsQuery.data
  const flags = flagsQuery.data?.flags.filter((flag) => flag.state !== 'archived') ?? []
  const linkedFlag = flags.find((flag) => flag.key === params?.flagKey)

  const orderedVariants = useMemo(() => {
    if (!result) return []
    const controlKey =
      linkedFlag?.default_variant ?? (result.variants.some((v) => v.variant === 'control') ? 'control' : null)
    return [...result.variants].sort((a, b) => {
      if (a.variant === controlKey) return -1
      if (b.variant === controlKey) return 1
      return a.variant.localeCompare(b.variant)
    })
  }, [result, linkedFlag])

  const exposedUnique = exposureQuery.data?.results[0]?.unique_users ?? null
  const statsUsers = result ? result.variants.reduce((sum, variant) => sum + variant.users, 0) : null
  const exposureMismatch =
    exposedUnique !== null && statsUsers !== null && statsUsers > 0
      ? Math.abs(exposedUnique - statsUsers) / statsUsers > 0.1
      : false

  const waitingForTraffic = resultsQuery.error instanceof ApiError && resultsQuery.error.status === 404

  return (
    <div className="space-y-4">
      <Card>
        <CardContent className="flex flex-wrap items-end gap-3 p-4">
          <div className="space-y-1.5">
            <Label>Flag (exposure source)</Label>
            <Select
              value={inputs.flagKey}
              onChange={(event) => setInputs((previous) => ({ ...previous, flagKey: event.target.value }))}
              className="w-56"
              aria-label="Flag key"
            >
              <option value="">— pick a flag —</option>
              {flags.map((flag) => (
                <option key={flag.key} value={flag.key}>
                  {flag.key}
                </option>
              ))}
            </Select>
          </div>
          <div className="space-y-1.5">
            <Label>Metric event</Label>
            <Input
              value={inputs.metric}
              onChange={(event) => setInputs((previous) => ({ ...previous, metric: event.target.value }))}
              placeholder="purchase_completed"
              className="w-56 font-mono text-xs"
              list="apdl-common-events"
            />
          </div>
          <div className="space-y-1.5">
            <Label>Method</Label>
            <Select
              value={inputs.method}
              onChange={(event) =>
                setInputs((previous) => ({ ...previous, method: event.target.value as AnalysisMethod }))
              }
              className="w-44"
              aria-label="Method"
            >
              <option value="sequential">sequential (peek-safe)</option>
              <option value="frequentist">frequentist</option>
              <option value="bayesian">bayesian</option>
            </Select>
          </div>
          <Button size="sm" onClick={run}>
            <Play />
            Compute
          </Button>
          {result ? (
            <Button variant="outline" size="sm" onClick={() => void resultsQuery.refetch()}>
              <RefreshCw />
              Refresh
            </Button>
          ) : null}
        </CardContent>
      </Card>

      {resultsQuery.isLoading ? <Skeleton className="h-64 w-full" /> : null}

      {waitingForTraffic ? (
        <EmptyState
          title="Waiting for traffic"
          description="No exposures or metric events found for this flag + metric yet."
        >
          {params ? (
            <Button variant="outline" size="sm" asChild>
              <Link to={`/flags/${encodeURIComponent(params.flagKey)}?tab=tester`}>
                Open the flag tester
              </Link>
            </Button>
          ) : null}
        </EmptyState>
      ) : resultsQuery.error ? (
        <ErrorState error={resultsQuery.error} onRetry={() => void resultsQuery.refetch()} />
      ) : null}

      {result ? (
        <div className="space-y-4">
          <div
            className={cn(
              'rounded-lg border p-4',
              result.is_significant && (result.effect_size ?? 0) > 0
                ? 'border-emerald-300 bg-emerald-50 dark:border-emerald-900 dark:bg-emerald-950/30'
                : result.is_significant
                  ? 'border-red-300 bg-red-50 dark:border-red-900 dark:bg-red-950/30'
                  : 'bg-muted/40',
            )}
          >
            <div className="flex flex-wrap items-center justify-between gap-2">
              <p className="text-sm">
                <span className="font-semibold">
                  {result.is_significant ? 'Statistically significant' : 'Not significant yet'}
                </span>{' '}
                — {result.recommendation}
              </p>
              <span className="flex items-center gap-2">
                {active && params ? (
                  <CurlButton
                    spec={experimentResultsCurl(serviceConnection(active, 'query'), experimentKey, params)}
                    title="Experiment results"
                  />
                ) : null}
                <span className="text-xs text-muted-foreground">
                  computed <RelativeTime value={new Date(resultsQuery.dataUpdatedAt).toISOString()} />
                </span>
              </span>
            </div>
          </div>

          <div className="grid items-start gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Variants</CardTitle>
                <CardDescription>
                  Per-user metric "{result.metric}" among users exposed to {result.flag_key}.
                </CardDescription>
              </CardHeader>
              <CardContent>
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>Variant</TableHead>
                      <TableHead className="text-right">Users</TableHead>
                      <TableHead className="text-right">Mean</TableHead>
                      <TableHead className="text-right">Stddev</TableHead>
                      <TableHead className="text-right">Total</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {orderedVariants.map((variant) => (
                      <TableRow key={variant.variant}>
                        <TableCell className="font-mono text-xs">{variant.variant}</TableCell>
                        <TableCell className="text-right tabular-nums">{variant.users.toLocaleString()}</TableCell>
                        <TableCell className="text-right tabular-nums">{variant.mean.toFixed(4)}</TableCell>
                        <TableCell className="text-right tabular-nums">{variant.stddev.toFixed(4)}</TableCell>
                        <TableCell className="text-right tabular-nums">{variant.total.toLocaleString()}</TableCell>
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Method — {result.method}</CardTitle>
              </CardHeader>
              <CardContent>
                <MethodBlock result={result} />
              </CardContent>
            </Card>
          </div>

          <div className="grid items-start gap-4 lg:grid-cols-2">
            <Card>
              <CardHeader>
                <CardTitle>Exposure context</CardTitle>
                <CardDescription>
                  Unique users with $feature_flag_exposure for this flag (last 90 days) — a sanity
                  check on the join the statistics ran on.
                </CardDescription>
              </CardHeader>
              <CardContent className="space-y-2 text-sm">
                <p className="tabular-nums">
                  Exposed users: <span className="font-medium">{exposedUnique?.toLocaleString() ?? '…'}</span>{' '}
                  · in statistics: <span className="font-medium">{statsUsers?.toLocaleString() ?? '—'}</span>
                </p>
                {exposureMismatch ? (
                  <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                    Counts differ by more than 10% — a data-quality hint (date windows, missing
                    user_ids, or exposures without metric joins).
                  </p>
                ) : null}
              </CardContent>
            </Card>

            <Card>
              <CardHeader>
                <CardTitle>Guardrail glance</CardTitle>
                <CardDescription>$frontend_error events, last 7 days.</CardDescription>
              </CardHeader>
              <CardContent>
                {errorQuery.data ? (
                  errorQuery.data.buckets.length > 0 ? (
                    <SimpleLineChart
                      height={160}
                      color="#ef4444"
                      data={errorQuery.data.buckets.map((bucket) => ({
                        label: bucket.bucket.slice(5, 10),
                        value: bucket.event_count,
                      }))}
                    />
                  ) : (
                    <p className="text-sm text-muted-foreground">No frontend errors recorded. 🎉</p>
                  )
                ) : (
                  <Skeleton className="h-32 w-full" />
                )}
              </CardContent>
            </Card>
          </div>
        </div>
      ) : null}
    </div>
  )
}
