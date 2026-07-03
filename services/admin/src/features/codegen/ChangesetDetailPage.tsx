// Per-changeset detail (/codegen/:id). The list view is a scannable summary;
// this page is the diagnostic surface — the full task + spec the agent was
// given, the lifecycle stage it reached, PR/CI/diff facts, and (crucially) the
// UNTRUNCATED failure reason for a tests_failed / error run, which is the one
// thing an operator needs to know why an autonomous PR never opened.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { AlertTriangle, ExternalLink, GitBranch } from 'lucide-react'
import { Link, useParams } from 'react-router-dom'
import { toast } from 'sonner'

import { abandonChangeset, getChangeset, mergeChangeset, revertChangeset } from '@/api/codegen'
import { ApiError } from '@/api/http'
import { TERMINAL_CHANGESET_STATUSES } from '@/api/schemas/codegen'
import type { Changeset } from '@/api/types/codegen'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { queryKeys } from '@/core/queryClient'
import { cn } from '@/lib/utils'
import { serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'
import { ChangesetStatusPill } from '@/features/codegen/ChangesetStatusPill'

const REFETCH_MS = 5000

// Happy-path lifecycle stages, in order. A failure status maps to the stage it
// died on (STAGE_OF); statuses that can fail anywhere map to -1 (no highlight).
const STAGE_LABELS = ['Queued', 'Clone', 'Edit', 'Test', 'Push', 'PR', 'CI', 'Merged'] as const
const STAGE_OF: Record<string, number> = {
  queued: 0,
  cloning: 1,
  editing: 2,
  testing: 3,
  tests_failed: 3,
  pushing: 4,
  pr_open: 5,
  ci_running: 6,
  ci_failed: 6,
  ci_passed: 6,
  waiting_approval: 6,
  merged: 7,
  abandoned: -1,
  error: -1,
}
const FAILED_STATUSES = new Set(['tests_failed', 'ci_failed', 'error'])

function statNumber(diffStat: Record<string, unknown>, key: string): number | null {
  const value = diffStat[key]
  return typeof value === 'number' ? value : null
}

// The spec the agent receives often carries a trailing JSON metadata blob
// (dependencies / effort / components / considerations) appended after the
// human prose. Split them so each renders in its natural shape.
function splitSpec(spec: string): { prose: string; meta: Record<string, unknown> | null } {
  const idx = spec.lastIndexOf('\n\n{')
  if (idx === -1) return { prose: spec.trim(), meta: null }
  try {
    const parsed: unknown = JSON.parse(spec.slice(idx + 2))
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return { prose: spec.slice(0, idx).trim(), meta: parsed as Record<string, unknown> }
    }
  } catch {
    // Trailing brace was not JSON — treat the whole thing as prose.
  }
  return { prose: spec.trim(), meta: null }
}

function metaList(meta: Record<string, unknown>, key: string): string[] {
  const value = meta[key]
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

function LifecycleStepper({ status }: { status: string }) {
  const failed = FAILED_STATUSES.has(status)
  const current = STAGE_OF[status] ?? -1
  return (
    <ol className="flex flex-wrap items-center gap-1.5">
      {STAGE_LABELS.map((label, index) => {
        const isFailedStage = failed && index === current
        const state =
          current === -1
            ? 'pending'
            : isFailedStage
              ? 'failed'
              : index < current
                ? 'done'
                : index === current
                  ? 'active'
                  : 'pending'
        return (
          <li key={label} className="flex items-center gap-1.5">
            {index > 0 ? <span className="text-muted-foreground/40">→</span> : null}
            <span
              className={cn(
                'inline-flex items-center rounded-full border px-2.5 py-1 text-xs font-medium',
                state === 'done' && 'border-emerald-300 text-emerald-700 dark:text-emerald-400',
                state === 'active' &&
                  'border-sky-400 bg-sky-50 text-sky-800 dark:bg-sky-950/40 dark:text-sky-300',
                state === 'failed' &&
                  'border-red-400 bg-red-50 text-red-800 dark:bg-red-950/40 dark:text-red-300',
                state === 'pending' && 'text-muted-foreground',
              )}
            >
              {label}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

function Fact({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="space-y-1">
      <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">{label}</p>
      <div className="text-sm">{children}</div>
    </div>
  )
}

export function ChangesetDetailPage() {
  const { id = '' } = useParams()
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  const ws = active as Workspace

  const query = useQuery<Changeset>({
    queryKey: queryKeys.changeset(active?.id ?? 'none', id),
    enabled: active !== null && id !== '',
    refetchInterval: (q) =>
      q.state.data && TERMINAL_CHANGESET_STATUSES.has(q.state.data.status) ? false : REFETCH_MS,
    queryFn: ({ signal }) => getChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id, { signal }),
  })

  const invalidate = () => {
    if (active) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.changeset(active.id, id) })
      void queryClient.invalidateQueries({ queryKey: queryKeys.changesets(active.id) })
    }
  }
  const onError = (fallback: string) => (error: Error) =>
    toast.error(error instanceof ApiError ? error.message : fallback)

  const merge = useMutation({
    mutationFn: () => mergeChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Merge requested')
      invalidate()
    },
    onError: onError('Merge failed'),
  })
  const abandon = useMutation({
    mutationFn: () => abandonChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Changeset abandoned')
      invalidate()
    },
    onError: onError('Abandon failed'),
  })
  const revert = useMutation({
    mutationFn: () => revertChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Revert PR requested')
      invalidate()
    },
    onError: onError('Revert failed'),
  })
  const busy = merge.isPending || abandon.isPending || revert.isPending

  if (query.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-40 w-full" />
      </div>
    )
  }
  if (query.isError) {
    const notFound = query.error instanceof ApiError && query.error.status === 404
    return notFound ? (
      <EmptyState title="Changeset not found" description="The codegen service has no record of this changeset id." />
    ) : (
      <ErrorState error={query.error} onRetry={() => void query.refetch()} />
    )
  }

  const cs: Changeset = query.data
  const mergeable =
    (cs.status === 'ci_passed' || cs.status === 'waiting_approval') &&
    (cs.ci_status === 'passed' || cs.ci_status === 'none')
  const files = statNumber(cs.diff_stat, 'files')
  const additions = statNumber(cs.diff_stat, 'additions')
  const deletions = statNumber(cs.diff_stat, 'deletions')
  const { prose, meta } = splitSpec(cs.task.spec)

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/codegen', label: 'Code changes' }}
        title={
          <span className="flex flex-wrap items-center gap-2">
            {cs.task.title}
            <ChangesetStatusPill status={cs.status} />
          </span>
        }
        description={
          <>
            <code className="font-mono text-xs">{cs.changeset_id}</code> · created{' '}
            <RelativeTime value={cs.created_at} /> · updated <RelativeTime value={cs.updated_at} />
          </>
        }
        actions={
          cs.status === 'merged' ? (
            <Button size="sm" variant="outline" disabled={busy} onClick={() => revert.mutate()}>
              Revert
            </Button>
          ) : (
            <>
              <Button size="sm" disabled={busy || !mergeable} onClick={() => merge.mutate()}>
                Merge
              </Button>
              <Button
                size="sm"
                variant="ghost"
                disabled={busy || TERMINAL_CHANGESET_STATUSES.has(cs.status)}
                onClick={() => abandon.mutate()}
              >
                Abandon
              </Button>
            </>
          )
        }
      />

      {cs.error ? (
        <Card className="border-red-400 dark:border-red-800">
          <CardHeader>
            <CardTitle className="flex items-center gap-2 text-red-700 dark:text-red-400">
              <AlertTriangle className="h-5 w-5" />
              Failure reason
            </CardTitle>
            <CardDescription>
              Why this run ended in <code className="font-mono">{cs.status}</code> without opening (or completing) a PR.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <pre className="max-h-96 overflow-auto rounded-md bg-muted p-3 text-xs leading-relaxed whitespace-pre-wrap break-words">
              {cs.error}
            </pre>
          </CardContent>
        </Card>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle>Lifecycle</CardTitle>
          <CardDescription>Stage this changeset reached in the autonomous pipeline.</CardDescription>
        </CardHeader>
        <CardContent>
          <LifecycleStepper status={cs.status} />
        </CardContent>
      </Card>

      <Card>
        <CardContent className="grid gap-4 p-4 sm:grid-cols-2 lg:grid-cols-3">
          <Fact label="Base branch">
            <code className="font-mono">{cs.base_branch ?? '—'}</code>
          </Fact>
          <Fact label="Work branch">
            {cs.branch ? (
              <span className="inline-flex items-center gap-1">
                <GitBranch className="h-3.5 w-3.5 text-muted-foreground" />
                <code className="font-mono break-all">{cs.branch}</code>
              </span>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Pull request">
            {cs.pr_url ? (
              <a
                href={cs.pr_url}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 underline"
              >
                #{cs.pr_number}
                <ExternalLink className="h-3 w-3" />
              </a>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="CI status">{cs.ci_status === 'none' ? 'no CI configured' : (cs.ci_status ?? '—')}</Fact>
          <Fact label="Diff">
            {files !== null ? (
              <span className="tabular-nums">
                {files} file{files === 1 ? '' : 's'}
                {additions !== null ? (
                  <span className="text-emerald-600 dark:text-emerald-400"> +{additions}</span>
                ) : null}
                {deletions !== null ? (
                  <span className="text-red-600 dark:text-red-400"> −{deletions}</span>
                ) : null}
              </span>
            ) : (
              '—'
            )}
          </Fact>
          <Fact label="Agent run">
            {cs.run_id ? (
              <Link to={`/agents/runs/${cs.run_id}`} className="font-mono text-xs underline">
                {cs.run_id.slice(0, 8)}…
              </Link>
            ) : (
              '—'
            )}
          </Fact>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Task</CardTitle>
          <CardDescription>The specification handed to the editing agent.</CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{prose}</p>

          {meta ? (
            <div className="grid gap-4 sm:grid-cols-2">
              {typeof meta.estimated_effort === 'string' ? (
                <Fact label="Estimated effort">
                  <span className="inline-flex items-center rounded-full border bg-muted px-2 py-0.5 text-xs font-medium">
                    {meta.estimated_effort}
                  </span>
                </Fact>
              ) : null}
              {(['dependencies', 'components_affected', 'technical_considerations'] as const).map((key) => {
                const items = metaList(meta, key)
                if (items.length === 0) return null
                return (
          <Fact label="Merge commit">
            {cs.merge_sha ? <code className="font-mono">{cs.merge_sha.slice(0, 12)}</code> : '—'}
          </Fact>
                  <Fact key={key} label={key.replace(/_/g, ' ')}>
                    <ul className="list-disc space-y-1 pl-4 text-sm text-muted-foreground">
                      {items.map((item, i) => (
                        <li key={i}>{item}</li>
                      ))}
                    </ul>
                  </Fact>
                )
              })}
            </div>
          ) : null}

          {cs.task.constraints.length > 0 ? (
            <Fact label="Constraints">
              <ul className="list-disc space-y-1 pl-4 text-sm text-muted-foreground">
                {cs.task.constraints.map((constraint, i) => (
                  <li key={i}>{constraint}</li>
                ))}
              </ul>
            </Fact>
          ) : null}
        </CardContent>
      </Card>
    </div>
  )
}
