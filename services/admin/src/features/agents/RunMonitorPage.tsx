// Run monitor + approvals (plan §5.6.2–3). Polling cadence per AD-5: 2s while
// running, 5s while waiting_approval, stop on terminal. Honest limitation
// until gap G3: the status API exposes only counts, so the approval panel
// cannot show WHAT is being approved — the UI says so explicitly.
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, Loader2, ShieldAlert } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { approveRun, runStatus, runStatusCurl } from '@/api/agents'
import { ApiError } from '@/api/http'
import { TERMINAL_RUN_STATUSES } from '@/api/schemas/agents'
import type { RunStatus } from '@/api/types/agents'
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
import { loadTrackedRuns, updateTrackedRunStatus } from '@/features/agents/runHistory'
import { RunStatusPill } from '@/features/agents/RunStatusPill'
import { useNow } from '@/lib/hooks'
import { parseServerDate } from '@/lib/format'
import { cn } from '@/lib/utils'

const PIPELINE = [
  'initializing',
  'behavior_analysis',
  'experiment_design',
  'personalization',
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

function ApprovalPanel({ run, onDecided }: { run: RunStatus; onDecided: () => void }) {
  const { active } = useWorkspace()
  const [comment, setComment] = useState('')
  const [submitting, setSubmitting] = useState<'approve' | 'reject' | null>(null)

  const decide = async (approved: boolean) => {
    if (!active) return
    if (!approved && comment.trim() === '') {
      toast.error('A comment is required when rejecting')
      return
    }
    setSubmitting(approved ? 'approve' : 'reject')
    try {
      await approveRun(serviceConnection(active, 'agents'), run.run_id, {
        approved,
        ...(comment.trim() ? { comment: comment.trim() } : {}),
      })
      toast.success(approved ? 'Approved — run resumes' : 'Rejected — run halts')
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
      setSubmitting(null)
    }
  }

  const agent = run.phase.replace(/_approval$/, '').replace(/_/g, ' ')

  return (
    <Card className="border-amber-400 dark:border-amber-700">
      <CardHeader>
        <CardTitle className="flex items-center gap-2">
          <ShieldAlert className="h-5 w-5 text-amber-500" />
          {agent} awaiting approval
        </CardTitle>
        <CardDescription>
          This run is gated by its autonomy level. Until the run-results endpoint lands (plan gap
          G3), the API exposes only counts — review the agents-service logs for the full payload
          being approved.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-3">
        <div className="space-y-1.5">
          <Label htmlFor="approval-comment">Comment (required when rejecting)</Label>
          <Input
            id="approval-comment"
            value={comment}
            onChange={(event) => setComment(event.target.value)}
            placeholder="Why approve / reject?"
          />
        </div>
        <div className="flex gap-2">
          <Button onClick={() => void decide(true)} disabled={submitting !== null}>
            {submitting === 'approve' ? <Loader2 className="animate-spin" /> : null}
            Approve
          </Button>
          <Button
            variant="destructive"
            onClick={() => void decide(false)}
            disabled={submitting !== null || comment.trim() === ''}
          >
            {submitting === 'reject' ? <Loader2 className="animate-spin" /> : null}
            Reject
          </Button>
        </div>
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

  useEffect(() => {
    if (active && run) updateTrackedRunStatus(active.id, run.run_id, run.status)
  }, [active, run])

  const tracked = active ? loadTrackedRuns(active.id).find((entry) => entry.run_id === runId) : null
  const requested = tracked ? new Set<string>(tracked.analysis_types) : null

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
              ? ' · requested agents unknown (run not triggered from this browser)'
              : ''}
          </CardDescription>
        </CardHeader>
        <CardContent>
          <PhaseStepper run={run} requested={requested} />
        </CardContent>
      </Card>

      {run.status === 'waiting_approval' ? (
        <ApprovalPanel run={run} onDecided={() => void statusQuery.refetch()} />
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
      <p className="text-xs text-muted-foreground">
        Counts are all the detail the status API exposes today — full insight and proposal payloads
        require the run-results endpoint (plan §8 G3).
      </p>

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
    </div>
  )
}
