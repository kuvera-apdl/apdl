// Decide — the sole approval surface (admin-console-purpose-ia.md). Every
// pending human decision across all runs, phrased as a question with evidence
// inline. Aggregates client-side today: list waiting runs, fetch each run's
// persisted results, and derive its decisions. When the dedicated
// GET /v1/agents/approvals/pending endpoint lands, only useDecisions changes.
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { FlaskConical, GitPullRequest, Package, ShieldCheck } from 'lucide-react'
import { useMemo } from 'react'
import { toast } from 'sonner'

import { approveRun, listRuns, runResults } from '@/api/agents'
import { ApiError } from '@/api/http'
import type { RunResults, RunStatus } from '@/api/types/agents'
import { DecisionCard } from '@/components/shared/DecisionCard'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { SectionHeading } from '@/components/shared/SectionHeading'
import { Skeleton } from '@/components/ui/skeleton'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'
import { AgentReadOnlyNote } from '@/features/agents/AgentAccessNotice'
import { decisionsForRun, type Decision } from '@/lib/gates'

const AGENT_ICON = {
  experiment_design: FlaskConical,
  feature_proposal: Package,
  code_implementation: GitPullRequest,
  personalization: ShieldCheck,
} as const

// Aggregate all pending decisions from every waiting run.
function useDecisions() {
  const { active, projectId } = useWorkspace()
  const conn = active ? serviceConnection(active, 'agents') : null

  const waitingQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'decide', 'waiting-runs'],
    enabled: Boolean(conn && projectId),
    refetchInterval: 10_000,
    queryFn: ({ signal }) =>
      listRuns(conn!, { projectId: projectId!, status: 'waiting_approval' }, { signal }),
  })

  const runs = waitingQuery.data?.runs ?? []
  const resultQueries = useQueries({
    queries: runs.map((run) => ({
      queryKey: [active?.id ?? 'none', 'decide', 'results', run.run_id],
      enabled: Boolean(conn),
      queryFn: ({ signal }: { signal: AbortSignal }) => runResults(conn!, run.run_id, { signal }),
    })),
  })

  const decisions = useMemo<Decision[]>(() => {
    return runs.flatMap((run, index) => {
      const results = (resultQueries[index]?.data as RunResults | undefined) ?? null
      return decisionsForRun(run, results)
    })
  }, [runs, resultQueries])

  return {
    decisions,
    runsById: Object.fromEntries(runs.map((r) => [r.run_id, r])) as Record<string, RunStatus>,
    isPending: waitingQuery.isPending,
    error: waitingQuery.error,
    refetch: () => void waitingQuery.refetch(),
    endpointMissing: waitingQuery.error instanceof ApiError && waitingQuery.error.status === 404,
  }
}

export function DecidePage() {
  const { active } = useWorkspace()
  const canApprove = hasWorkspaceRole(active, 'agents:approve')
  const queryClient = useQueryClient()
  const { decisions, isPending, error, refetch, endpointMissing } = useDecisions()

  const decide = useMutation({
    mutationFn: ({ decision, approved }: { decision: Decision; approved: boolean }) =>
      approveRun(serviceConnection(active!, 'agents'), decision.runId, {
        decisions: [{ item_id: decision.itemId, approved }],
      }),
    onSuccess: (res, { approved }) => {
      const opened = (res.opened_changesets ?? []).length
      const forked = (res.forked_runs ?? []).length
      const errors = res.errors ?? []
      const summary =
        `${approved ? 'Approved' : 'Rejected'} — run resumes` +
        (forked ? ` · ${forked} PR run(s) forked` : '') +
        (opened ? ` · ${opened} PR(s) opened` : '')
      if (errors.length > 0) {
        toast.warning(`${summary} · ${errors.length} deployment error(s)`, {
          description: errors.join(' · '),
        })
      } else {
        toast.success(summary)
      }
      void queryClient.invalidateQueries({ queryKey: [active?.id ?? 'none', 'decide'] })
    },
    onError: (err) => {
      if (err instanceof ApiError && err.status === 400) {
        toast.info('This run is no longer awaiting a decision — refreshing.')
        void queryClient.invalidateQueries({ queryKey: [active?.id ?? 'none', 'decide'] })
      } else {
        toast.error(err instanceof ApiError ? err.message : 'Decision failed')
      }
    },
  })

  const actionsFor = (decision: Decision) => {
    if (!canApprove) return []
    const accept = { experiment_design: 'Approve design', feature_proposal: 'Make permanent', code_implementation: 'Open PR' }
    return [
      {
        label: accept[decision.agent as keyof typeof accept] ?? 'Approve',
        variant: 'default' as const,
        disabled: decide.isPending,
        onClick: () => decide.mutate({ decision, approved: true }),
      },
      {
        label: 'Reject',
        variant: 'outline' as const,
        disabled: decide.isPending,
        onClick: () => decide.mutate({ decision, approved: false }),
      },
    ]
  }

  return (
    <div className="space-y-4">
      <PageHeader
        title="Decide"
        description={
          decisions.length > 0
            ? canApprove
              ? `${decisions.length} decision${decisions.length === 1 ? '' : 's'} waiting — everything the loop can't do without you`
              : `${decisions.length} operator decision${decisions.length === 1 ? '' : 's'} pending — read-only in this workspace`
            : canApprove
              ? 'Everything the loop needs a human for lands here.'
              : 'Pending operator decisions remain visible here without approval controls.'
        }
      />

      {!canApprove ? (
        <AgentReadOnlyNote>
          Decisions are read-only. Submitting a verdict and resuming a run requires agents:approve.
        </AgentReadOnlyNote>
      ) : null}

      {isPending ? <Skeleton className="h-40 w-full" /> : null}

      {error && !endpointMissing ? <ErrorState error={error} onRetry={refetch} /> : null}
      {endpointMissing ? (
        <EmptyState
          title="Agents service too old"
          description="This agents service predates the runs-list endpoint, so decisions can't be aggregated here."
        />
      ) : null}

      {!isPending && !error && decisions.length === 0 ? (
        <EmptyState
          icon={<ShieldCheck className="h-8 w-8 text-emerald-500" />}
          title={canApprove ? 'Nothing needs you' : 'No pending operator decisions'}
          description={
            canApprove
              ? 'The loop keeps measuring on its own. New decisions will appear here the moment an agent needs a human.'
              : 'This workspace can inspect decisions when an operator action is pending.'
          }
        />
      ) : null}

      {decisions.length > 0 ? (
        <div className="space-y-2">
          <SectionHeading
            title={canApprove ? 'Waiting on you' : 'Waiting on an operator'}
            count={decisions.length}
          />
          {decisions.map((decision, index) => (
            <DecisionCard
              key={`${decision.runId}:${decision.itemId}`}
              icon={AGENT_ICON[decision.agent as keyof typeof AGENT_ICON] ?? ShieldCheck}
              question={decision.question}
              stage={decision.stage}
              evidence={decision.evidence}
              detail={decision.detail}
              actions={actionsFor(decision)}
              detailLink={{ to: `/agents/runs/${encodeURIComponent(decision.runId)}`, label: 'Full run' }}
              emphasis={index === 0}
            />
          ))}
        </div>
      ) : null}
    </div>
  )
}
