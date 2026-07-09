// Run monitor + approvals (plan §5.6.2–3). Polling cadence per AD-5: 2s while
// running, 5s while waiting_approval, stop on terminal. Honest limitation
// until gap G3: the status API exposes only counts, so the approval panel
// cannot show WHAT is being approved — the UI says so explicitly.
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { approveRun, runResults, runResultsCurl, runStatus, runStatusCurl } from '@/api/agents'
import { ApiError } from '@/api/http'
import { TERMINAL_RUN_STATUSES } from '@/api/schemas/agents'
import type { RunResults, RunStatus } from '@/api/types/agents'
import { CurlButton } from '@/components/shared/CurlButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { ResultCard, ResultList } from '@/features/agents/ResultCards'
import { AgentsInspectorSection } from '@/features/agents/AgentInspector'
import { RunAuditSection } from '@/features/agents/RunAuditSection'
import { RunStatusPill } from '@/features/agents/RunStatusPill'
import { useNow } from '@/lib/hooks'
import { parseServerDate } from '@/lib/format'
import { cn } from '@/lib/utils'

// personalization is parked (disabled server-side), so it's not a pipeline
// step. Historical runs that produced personalizations still show them in the
// Outputs card below — this only drops the stepper node.
const PIPELINE = [
  'initializing',
  'behavior_analysis',
  'experiment_design',
  'feature_proposal',
  'done',
] as const

function stepState(step: string, run: RunStatus): 'done' | 'active' | 'gate' | 'pending' {
  const phase = run.phase.replace(/_approval$/, '')
  const gated = run.phase.endsWith('_approval')
  const stepIndex = PIPELINE.indexOf(step as (typeof PIPELINE)[number])
  const phaseIndex = PIPELINE.indexOf(phase as (typeof PIPELINE)[number])
  if (phase === 'resuming') return stepIndex <= PIPELINE.indexOf('feature_proposal') ? 'done' : 'pending'
  if (phaseIndex === -1) return 'pending'
  if (stepIndex < phaseIndex) return 'done'
  if (stepIndex === phaseIndex) return gated ? 'gate' : 'active'
  return 'pending'
}

function PhaseStepper({ run, requested }: { run: RunStatus; requested: Set<string> | null }) {
  return (
    <ol className="flex flex-wrap items-center gap-2">
      {PIPELINE.map((step, index) => {
        const state = stepState(step, run)
        const skipped =
          requested !== null &&
          step !== 'initializing' &&
          step !== 'done' &&
          !requested.has(step)
        return (
          <li key={step} className="flex items-center gap-2">
            {index > 0 ? <span className="text-muted-foreground/50">→</span> : null}
            <span
              className={cn(
                'inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-xs font-medium',
                state === 'done' && 'border-emerald-300 text-emerald-700 dark:text-emerald-400',
                state === 'active' && 'border-sky-400 bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300',
                state === 'gate' &&
                  'animate-pulse border-amber-400 bg-amber-50 text-amber-800 dark:bg-amber-950/40 dark:text-amber-300',
                state === 'pending' && 'text-muted-foreground',
                skipped && 'opacity-50 line-through',
              )}
              title={skipped ? 'Not requested for this run' : undefined}
            >
              {state === 'gate' ? <ShieldAlert className="h-3.5 w-3.5" /> : null}
              {state === 'done' ? <CheckCircle2 className="h-3.5 w-3.5" /> : null}
              {step.replace(/_/g, ' ')}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

const GATED_RESULT_KEYS = {
  experiment_design: 'experiment_designs',
  personalization: 'personalizations',
  feature_proposal: 'feature_proposals',
  behavior_analysis: 'insights',
  code_implementation: 'changesets',
} as const

// Map a gated agent to its ResultCard renderer kind (most align 1:1).
const GATE_CARD_KIND = {
  experiment_design: 'experiment_design',
  personalization: 'personalization',
  feature_proposal: 'feature_proposal',
  behavior_analysis: 'insight',
  code_implementation: 'changeset',
} as const

function gatedItems(run: RunStatus, results: RunResults | null): { items: unknown[]; kind: keyof typeof GATED_RESULT_KEYS } | null {
  if (!results) return null
  const agent = run.phase.replace(/_approval$/, '') as keyof typeof GATED_RESULT_KEYS
  const key = GATED_RESULT_KEYS[agent]
  if (!key) return null
  return { items: results[key], kind: agent }
}

// The stable id used to match a per-item decision to a gated item — mirrors
// the server's _item_id (experiment_id / flag_config.key for designs,
// proposal_id for proposals). Falls back to a positional id the server accepts
// for a single unkeyed item.
function itemId(item: unknown, kind: string, index: number): string {
  const rec = typeof item === 'object' && item !== null ? (item as Record<string, unknown>) : {}
  const flag = rec.flag_config as Record<string, unknown> | undefined
  const raw = kind === 'experiment_design' ? (rec.experiment_id ?? flag?.key) : rec.proposal_id
  return typeof raw === 'string' && raw.trim() !== '' ? raw : `__index_${index}`
}

function ApprovalPanel({
  run,
  results,
  onDecided,
}: {
  run: RunStatus
  results: RunResults | null
  onDecided: () => void
}) {
  const { active } = useWorkspace()
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState(false)

  const agent = run.phase.replace(/_approval$/, '').replace(/_/g, ' ')
  const gated = gatedItems(run, results)
  const items = gated?.items ?? []
  const kind = gated?.kind
  const hasItems = items.length > 0 && kind !== undefined

  const ids = items.map((item, index) => itemId(item, kind ?? '', index))
  // Per-item verdicts, defaulting to approve; re-seeded when the item set changes.
  const [decisions, setDecisions] = useState<Record<string, boolean>>({})
  const idsKey = ids.join('|')
  useEffect(() => {
    setDecisions((prev) => Object.fromEntries(ids.map((id) => [id, prev[id] ?? true])))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [idsKey])

  const isApproved = (id: string) => decisions[id] !== false
  const anyRejected = hasItems && ids.some((id) => !isApproved(id))
  const approvedCount = ids.filter(isApproved).length

  const submit = async (body: { decisions?: { item_id: string; approved: boolean }[]; approved?: boolean }, needsComment: boolean) => {
    if (!active) return
    if (needsComment && comment.trim() === '') {
      toast.error('A comment is required when rejecting')
      return
    }
    setSubmitting(true)
    try {
      const res = await approveRun(serviceConnection(active, 'agents'), run.run_id, {
        ...body,
        ...(comment.trim() ? { comment: comment.trim() } : {}),
      })
      const forked = (res.forked_runs ?? []).length
      const opened = (res.opened_changesets ?? []).length
      toast.success(
        `${res.approved_count ?? 0} approved, ${res.rejected_count ?? 0} rejected — run resumes` +
          (forked ? ` · ${forked} PR run(s) forked` : '') +
          (opened ? ` · ${opened} PR(s) opened` : ''),
      )
      onDecided()
    } catch (error) {
      if (error instanceof ApiError && error.status === 400) {
        // Someone else acted, or the supervisor moved on — re-poll (§5.6.3).
        toast.info('This run is no longer awaiting approval — refreshing.')
        onDecided()
      } else {
        toast.error(error instanceof ApiError ? error.message : 'Decision failed')
      }
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <Card className="border-amber-400 dark:border-amber-700">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="h-5 w-5 text-amber-500" />
          {agent} awaiting approval
        </CardTitle>
        <CardDescription>
          {hasItems
            ? 'Approve or reject each item, then submit. Each approved proposal opens its own PR; approved experiments deploy individually.'
            : 'This run is gated by its autonomy level. No persisted payload is available for this phase — review the agents-service logs before deciding.'}
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        {hasItems ? (
          <div className="space-y-2">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              What you&apos;re approving ({items.length})
            </p>
            {items.map((item, index) => {
              const id = ids[index]!
              const approved = isApproved(id)
              return (
                <div key={id} className="flex items-start gap-2">
                  <div className="min-w-0 flex-1">
                    <ResultCard item={item} kind={GATE_CARD_KIND[kind!]} />
                  </div>
                  <div className="flex shrink-0 flex-col gap-1 pt-1">
                    <Button
                      size="sm"
                      variant={approved ? 'default' : 'outline'}
                      onClick={() => setDecisions((prev) => ({ ...prev, [id]: true }))}
                    >
                      Approve
                    </Button>
                    <Button
                      size="sm"
                      variant={approved ? 'outline' : 'destructive'}
                      onClick={() => setDecisions((prev) => ({ ...prev, [id]: false }))}
                    >
                      Reject
                    </Button>
                  </div>
                </div>
              )
            })}
          </div>
        ) : null}
        <div className="space-y-1.5">
          <Label htmlFor="approval-comment">Comment (required when rejecting)</Label>
          <Input
            id="approval-comment"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="Why approve / reject?"
          />
        </div>
        {hasItems ? (
          <div className="flex items-center gap-3">
            <Button
              onClick={() =>
                void submit(
                  { decisions: ids.map((id) => ({ item_id: id, approved: isApproved(id) })) },
                  anyRejected,
                )
              }
              disabled={submitting}
            >
              {submitting ? <Loader2 className="animate-spin" /> : null}
              Submit decisions
            </Button>
            <span className="text-xs text-muted-foreground">
              {approvedCount} of {ids.length} approved
            </span>
          </div>
        ) : (
          <div className="flex gap-2">
            <Button onClick={() => void submit({ approved: true }, false)} disabled={submitting}>
              {submitting ? <Loader2 className="animate-spin" /> : null}
              Approve
            </Button>
            <Button
              variant="destructive"
              onClick={() => void submit({ approved: false }, true)}
              disabled={submitting || comment.trim() === ''}
            >
              Reject
            </Button>
          </div>
        )}
      </CardContent>
    </Card>
  )
}

export function RunMonitorPage() {
  const { runId = '' } = useParams()
  const { active } = useWorkspace()
  const now = useNow(1000)

  const statusQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run', runId],
    enabled: active !== null && runId !== '',
    queryFn: ({ signal }) => runStatus(serviceConnection(active!, 'agents'), runId, { signal }),
    refetchInterval: (query) => {
      const status = query.state.data?.status
      if (!status) return 2000
      if (TERMINAL_RUN_STATUSES.has(status)) return false
      return status === 'waiting_approval' ? 5000 : 2000
    },
  })

  const run = statusQuery.data

  const resultsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run', runId, 'results'],
    enabled: active !== null && runId !== '' && run !== undefined,
    queryFn: ({ signal }) => runResults(serviceConnection(active!, 'agents'), runId, { signal }),
    refetchInterval: run && !TERMINAL_RUN_STATUSES.has(run.status) ? 5000 : false,
  })
  const results = resultsQuery.data ?? null

  // The requested agents come from the run itself (server-side). An older
  // backend omits them → null → the stepper shows all steps as generic.
  const requested = run && run.analysis_types?.length ? new Set(run.analysis_types) : null

  if (statusQuery.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }
  if (statusQuery.error) {
    const notFound = statusQuery.error instanceof ApiError && statusQuery.error.status === 404
    return notFound ? (
      <EmptyState
        title="Run not found"
        description="The agents service has no record of this run id."
      />
    ) : (
      <ErrorState error={statusQuery.error} onRetry={() => void statusQuery.refetch()} />
    )
  }
  if (!run) return null

  const startedAt = parseServerDate(run.started_at)
  const elapsedSeconds = startedAt ? Math.max(0, Math.round((now - startedAt.getTime()) / 1000)) : null
  const terminal = TERMINAL_RUN_STATUSES.has(run.status)

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/agents', label: 'Agent runs' }}
        title={
          <span className="flex flex-wrap items-center gap-2">
            <code className="font-mono text-base">{run.run_id.slice(0, 8)}…</code>
            <RunStatusPill status={run.status} />
          </span>
        }
        description={
          <>
            started <RelativeTime value={run.started_at} /> · updated{' '}
            <RelativeTime value={run.updated_at} />
            {elapsedSeconds !== null && !terminal ? ` · elapsed ${elapsedSeconds}s` : ''}
          </>
        }
        actions={
          active ? (
            <CurlButton spec={runStatusCurl(serviceConnection(active, 'agents'), runId)} title="Run status" />
          ) : null
        }
      />

      <Card>
        <CardHeader>
          <CardTitle>Pipeline</CardTitle>
          <CardDescription>
            Phase: <code className="font-mono">{run.phase}</code>
            {requested === null
              ? ' · requested agents unknown (older agents service)'
              : ''}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PhaseStepper run={run} requested={requested} />
        </CardContent>
      </Card>

      <AgentsInspectorSection runId={runId} run={run} results={results} />

      {run.status === 'waiting_approval' ? (
        <ApprovalPanel
          run={run}
          results={results}
          onDecided={() => {
            void statusQuery.refetch()
            void resultsQuery.refetch()
          }}
        />
      ) : null}

      <div className="grid gap-4 sm:grid-cols-2">
        <Card>
          <CardContent className="space-y-1 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">Insights</p>
            <p className="text-2xl font-semibold tabular-nums">{run.insights_count}</p>
          </CardContent>
        </Card>
        <Card>
          <CardContent className="space-y-1 p-4">
            <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
              Experiment designs
            </p>
            <p className="text-2xl font-semibold tabular-nums">{run.experiments_count}</p>
          </CardContent>
        </Card>
      </div>
      {resultsQuery.error ? (
        <p className="text-xs text-muted-foreground">
          Full payloads unavailable — this agents service predates the run-results endpoint (plan
          §8 G3); counts are all the status API exposes.
        </p>
      ) : null}

      {results &&
      (results.insights.length > 0 ||
        results.experiment_designs.length > 0 ||
        results.personalizations.length > 0 ||
        results.feature_proposals.length > 0 ||
        Object.keys(results.custom_outputs ?? {}).length > 0) ? (
        <Card>
          <CardHeader className="flex-row items-start justify-between space-y-0">
            <div className="space-y-1.5">
              <CardTitle>Outputs</CardTitle>
              <CardDescription>Persisted per agent at phase completion.</CardDescription>
            </div>
            {active ? (
              <CurlButton
                spec={runResultsCurl(serviceConnection(active, 'agents'), runId)}
                title="Run results"
              />
            ) : null}
          </CardHeader>
          <CardContent className="space-y-4">
            <ResultList label="Insights" items={results.insights} kind="insight" />
            <ResultList label="Experiment designs" items={results.experiment_designs} kind="experiment_design" />
            <ResultList label="Personalizations" items={results.personalizations} kind="personalization" />
            <ResultList label="Feature proposals" items={results.feature_proposals} kind="feature_proposal" />
            {Object.entries(results.custom_outputs ?? {}).map(([produces, items]) => (
              <ResultList
                key={produces}
                label={`${produces.replace(/_/g, ' ')} (custom)`}
                items={items}
                kind="custom"
              />
            ))}
          </CardContent>
        </Card>
      ) : null}

      {terminal ? (
        <Card>
          <CardContent className="space-y-2 p-4 text-sm">
            {run.status === 'completed' ? (
              <p>
                Run completed. Agent-created changes land as flags and experiments —{' '}
                <Link to="/flags" className="font-medium underline underline-offset-4">
                  review recent flags
                </Link>{' '}
                and{' '}
                <Link to="/experiments" className="font-medium underline underline-offset-4">
                  experiments
                </Link>
                .
              </p>
            ) : run.status === 'rejected' ? (
              <p>Run halted by rejection.</p>
            ) : (
              <p>
                Run finished with errors — check the service logs:{' '}
                <code className="font-mono">scripts/dev.sh logs agents</code>
              </p>
            )}
          </CardContent>
        </Card>
      ) : null}

      <RunAuditSection runId={runId} />
    </div>
  )
}
