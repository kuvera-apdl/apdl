import { RefreshCw } from 'lucide-react'
import { Link } from 'react-router-dom'

import { experimentResultsCurl } from '@/api/experiments'
import type {
  ExperimentAnalysisInsufficient,
  ExperimentArmResult,
  ExperimentComparison,
} from '@/api/types/experiments'
import { CurlButton } from '@/components/shared/CurlButton'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useExperimentResultsQuery } from '@/features/experiments/hooks'
import { ExperimentStatusPill } from '@/features/experiments/StatusPill'
import { formatDateTime } from '@/lib/format'
import { cn } from '@/lib/utils'

const INSUFFICIENT_COPY: Record<
  ExperimentAnalysisInsufficient['reason'],
  { title: string; description: string }
> = {
  experiment_not_started: {
    title: 'Experiment has not started',
    description: 'The configured analysis window has not begun, so there is no attributable traffic yet.',
  },
  no_exposures: {
    title: 'No attributable exposures',
    description: 'No actors were attributed to a declared experiment arm in the authoritative window.',
  },
  underpowered_arms: {
    title: 'One or more arms need more traffic',
    description: 'Every declared arm must reach the minimum sample size before comparisons are computed.',
  },
  non_finite_statistics: {
    title: 'Statistics could not be represented safely',
    description: 'The service returned a typed insufficient-data result instead of non-finite statistics.',
  },
}

function formatRate(value: number): string {
  return `${(value * 100).toFixed(2)}%`
}

function formatDifference(value: number): string {
  const prefix = value > 0 ? '+' : ''
  return `${prefix}${(value * 100).toFixed(2)} pp`
}

function formatPValue(value: number): string {
  return value === 0 ? '0' : value.toExponential(3)
}

function ConfidenceIntervalBar({ interval }: { interval: [number, number] }) {
  const [low, high] = interval
  const domain = Math.max(Math.abs(low), Math.abs(high), 0.0001) * 1.25
  const toPct = (value: number) => ((value + domain) / (2 * domain)) * 100
  const crossesZero = low <= 0 && high >= 0

  return (
    <div className="min-w-44 space-y-1">
      <div className="relative h-2.5 w-full rounded-full bg-muted">
        <div className="absolute inset-y-0 w-px bg-foreground/50" style={{ left: '50%' }} title="zero" />
        <div
          className={cn(
            'absolute inset-y-0.5 rounded-full',
            crossesZero ? 'bg-amber-500/70' : 'bg-emerald-500/80',
          )}
          style={{ left: `${toPct(low)}%`, width: `${Math.max(1, toPct(high) - toPct(low))}%` }}
        />
      </div>
      <p className="text-xs tabular-nums text-muted-foreground">
        [{formatDifference(low)}, {formatDifference(high)}]
      </p>
    </div>
  )
}

function MetadataItem({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <dt className="text-xs uppercase tracking-wide text-muted-foreground">{label}</dt>
      <dd className="text-sm font-medium">{children}</dd>
    </div>
  )
}

function ArmsTable({ arms, controlVariant }: { arms: ExperimentArmResult[]; controlVariant: string }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>Variant summaries</CardTitle>
        <CardDescription>First-exposure actor attribution and binary metric conversions.</CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Variant</TableHead>
              <TableHead>Role</TableHead>
              <TableHead className="text-right">Actors</TableHead>
              <TableHead className="text-right">Conversions</TableHead>
              <TableHead className="text-right">Conversion rate</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {arms.map((arm) => (
              <TableRow key={arm.variant}>
                <TableCell className="font-mono text-xs">{arm.variant}</TableCell>
                <TableCell>
                  {arm.variant === controlVariant ? <Badge variant="secondary">control</Badge> : 'treatment'}
                </TableCell>
                <TableCell className="text-right tabular-nums">{arm.sample_size.toLocaleString()}</TableCell>
                <TableCell className="text-right tabular-nums">{arm.conversions.toLocaleString()}</TableCell>
                <TableCell className="text-right tabular-nums">{formatRate(arm.conversion_rate)}</TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

function ComparisonsTable({ comparisons }: { comparisons: ExperimentComparison[] }) {
  return (
    <Card>
      <CardHeader>
        <CardTitle>All treatment comparisons</CardTitle>
        <CardDescription>
          Every treatment is compared with control; adjusted p-values use the Bonferroni correction.
        </CardDescription>
      </CardHeader>
      <CardContent>
        <Table>
          <TableHeader>
            <TableRow>
              <TableHead>Treatment vs control</TableHead>
              <TableHead className="text-right">Rates</TableHead>
              <TableHead className="text-right">Difference</TableHead>
              <TableHead>Confidence interval</TableHead>
              <TableHead className="text-right">Raw p</TableHead>
              <TableHead className="text-right">Adjusted p</TableHead>
              <TableHead>Significance</TableHead>
            </TableRow>
          </TableHeader>
          <TableBody>
            {comparisons.map((comparison) => (
              <TableRow key={comparison.treatment_variant}>
                <TableCell className="font-mono text-xs">
                  {comparison.treatment_variant} vs {comparison.control_variant}
                </TableCell>
                <TableCell className="text-right text-xs tabular-nums">
                  {formatRate(comparison.treatment_rate)} vs {formatRate(comparison.control_rate)}
                </TableCell>
                <TableCell
                  className={cn(
                    'text-right font-medium tabular-nums',
                    comparison.rate_difference > 0
                      ? 'text-emerald-600'
                      : comparison.rate_difference < 0
                        ? 'text-red-600'
                        : '',
                  )}
                >
                  {formatDifference(comparison.rate_difference)}
                </TableCell>
                <TableCell>
                  <ConfidenceIntervalBar interval={comparison.confidence_interval} />
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {formatPValue(comparison.raw_p_value)}
                </TableCell>
                <TableCell className="text-right font-mono text-xs">
                  {formatPValue(comparison.adjusted_p_value)}
                </TableCell>
                <TableCell>
                  <Badge variant={comparison.is_significant ? 'default' : 'outline'}>
                    {comparison.is_significant ? 'significant' : 'not significant'}
                  </Badge>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </CardContent>
    </Card>
  )
}

export function ExperimentResultsTab({ experimentKey }: { experimentKey: string }) {
  const { active, projectId } = useWorkspace()
  const resultsQuery = useExperimentResultsQuery(experimentKey)
  const result = resultsQuery.data

  if (resultsQuery.isPending) return <Skeleton className="h-72 w-full" />
  if (resultsQuery.error) {
    return <ErrorState error={resultsQuery.error} onRetry={() => void resultsQuery.refetch()} />
  }
  if (!result) return null

  const insufficient = result.analysis_status === 'insufficient_data'
    ? INSUFFICIENT_COPY[result.reason]
    : null

  return (
    <div className="space-y-4">
      <Card>
        <CardHeader className="gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="space-y-1.5">
            <CardTitle>Authoritative experiment analysis</CardTitle>
            <CardDescription>
              Flag, metric, variants, control, and time window are resolved from Config—not supplied by this page.
            </CardDescription>
          </div>
          <div className="flex items-center gap-2">
            {active && projectId ? (
              <CurlButton
                spec={experimentResultsCurl(
                  serviceConnection(active, 'query'),
                  experimentKey,
                  { projectId },
                )}
                title="Experiment results"
              />
            ) : null}
            <Button variant="outline" size="sm" onClick={() => void resultsQuery.refetch()}>
              <RefreshCw />
              Refresh
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          <dl className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
            <MetadataItem label="Experiment">
              <code>{result.experiment_key}</code>
            </MetadataItem>
            <MetadataItem label="Backing flag">
              <Link className="font-mono text-primary underline-offset-4 hover:underline" to={`/flags/${encodeURIComponent(result.flag_key)}`}>
                {result.flag_key}
              </Link>
            </MetadataItem>
            <MetadataItem label="Status">
              <ExperimentStatusPill status={result.experiment_status} />
            </MetadataItem>
            <MetadataItem label="Analysis state">
              <Badge variant={result.analysis_status === 'ready' ? 'default' : 'secondary'}>
                {result.analysis_status}
              </Badge>
            </MetadataItem>
            <MetadataItem label="Metric event">
              <code>{result.metric_event}</code>
            </MetadataItem>
            <MetadataItem label="Control variant">
              <code>{result.control_variant}</code>
            </MetadataItem>
            <MetadataItem label="Authoritative window">
              {formatDateTime(result.start_date)} – {formatDateTime(result.end_date)}
            </MetadataItem>
            <MetadataItem label="Config version">v{result.config_version}</MetadataItem>
          </dl>
          <p className="text-xs text-muted-foreground">
            Fetched <RelativeTime value={new Date(resultsQuery.dataUpdatedAt).toISOString()} />.
            Results are read-only statistical evidence and do not trigger or recommend ship/rollback actions.
          </p>
        </CardContent>
      </Card>

      {insufficient && result.analysis_status === 'insufficient_data' ? (
        <Card className="border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/30">
          <CardHeader>
            <CardTitle>Insufficient data — {insufficient.title}</CardTitle>
            <CardDescription>{insufficient.description}</CardDescription>
          </CardHeader>
          <CardContent className="space-y-2 text-sm">
            <p>
              Minimum sample size per arm:{' '}
              <span className="font-medium tabular-nums">{result.minimum_sample_size_per_arm}</span>
            </p>
            {result.underpowered_variants.length > 0 ? (
              <p>
                Underpowered variants:{' '}
                <span className="font-mono text-xs">{result.underpowered_variants.join(', ')}</span>
              </p>
            ) : null}
          </CardContent>
        </Card>
      ) : null}

      <ArmsTable arms={result.arms} controlVariant={result.control_variant} />

      {result.analysis_status === 'ready' ? (
        <>
          <Card>
            <CardContent className="flex flex-wrap items-center gap-x-6 gap-y-2 p-4 text-sm">
              <span>
                Significance level:{' '}
                <strong className="tabular-nums">{result.significance_level}</strong>
              </span>
              <span>
                Multiple-comparison correction: <strong>{result.correction}</strong>
              </span>
              <span>
                Comparisons: <strong className="tabular-nums">{result.comparisons.length}</strong>
              </span>
            </CardContent>
          </Card>
          <ComparisonsTable comparisons={result.comparisons} />
        </>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Attribution quality</CardTitle>
          <CardDescription>Actor counts reported by the authoritative first-exposure analysis.</CardDescription>
        </CardHeader>
        <CardContent className="grid gap-4 sm:grid-cols-2">
          <MetadataItem label="Crossover actors">
            {result.crossover_actors.toLocaleString()}
          </MetadataItem>
          <MetadataItem label="Unknown-variant actors">
            {result.unknown_variant_actors.toLocaleString()}
          </MetadataItem>
        </CardContent>
      </Card>
    </div>
  )
}
