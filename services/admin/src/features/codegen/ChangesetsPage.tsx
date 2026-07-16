// Codegen changesets: the autonomous PRs the loop opens on connected repos.
// Lists changesets for the active workspace and observes GitHub-owned CI/merge.
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import {
  abandonChangeset,
  listChangesets,
  retryChangeset,
  revertChangeset,
} from '@/api/codegen'
import { ApiError } from '@/api/http'
import type { Changeset } from '@/api/types/codegen'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'
import {
  ChangesetStatusPill,
  ExternalCIStatusPill,
  GitHubPRStatusPill,
} from '@/features/codegen/ChangesetStatusPill'
import { GitHubConnectionCard } from '@/features/codegen/GitHubConnectionCard'

const REFETCH_MS = 5000

export function ChangesetsPage() {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  // This route is behind RequireAuth, so `active` is non-null when rendered.
  const ws = active as Workspace
  const projectId = active?.projectId ?? ''

  const query = useQuery({
    queryKey: active ? queryKeys.changesets(active.id) : ['none', 'changesets'],
    enabled: active !== null && projectId !== '',
    refetchInterval: REFETCH_MS,
    queryFn: () =>
      listChangesets(serviceConnection(ws, 'codegen'), { projectId }),
  })

  const invalidate = () => {
    if (active) void queryClient.invalidateQueries({ queryKey: queryKeys.changesets(active.id) })
  }
  const onError = (fallback: string) => (error: Error) =>
    toast.error(error instanceof ApiError ? error.message : fallback)

  const abandon = useMutation({
    mutationFn: (id: string) =>
      abandonChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Changeset abandoned')
      invalidate()
    },
    onError: onError('Abandon failed'),
  })
  const revert = useMutation({
    mutationFn: (id: string) =>
      revertChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Revert PR requested')
      invalidate()
    },
    onError: onError('Revert failed'),
  })
  const retry = useMutation({
    mutationFn: (id: string) =>
      retryChangeset(serviceConnection(ws, 'codegen'), id),
    onSuccess: () => {
      toast.success('Retry started')
      invalidate()
    },
    onError: onError('Retry failed'),
  })
  const busy = abandon.isPending || revert.isPending || retry.isPending

  return (
    <div className="space-y-6">
      <PageHeader
        title="Code changes"
        description="Autonomous PRs with GitHub-owned CI, review, and merge status. Polled every 5s."
      />
      <GitHubConnectionCard />
      {query.isPending ? (
        <Skeleton className="h-48 w-full" />
      ) : query.isError ? (
        <ErrorState error={query.error} onRetry={() => void query.refetch()} />
      ) : query.data.length === 0 ? (
        <EmptyState
          title="No changesets yet"
          description="Approve a feature proposal to kick off an autonomous pull request."
        />
      ) : (
        <Card>
          <CardContent className="p-0">
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Task</TableHead>
                  <TableHead>Lifecycle</TableHead>
                  <TableHead>GitHub PR</TableHead>
                  <TableHead>External CI</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="text-right">Actions</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.map((cs) => (
                  <ChangesetRow
                    key={cs.changeset_id}
                    cs={cs}
                    busy={busy}
                    onAbandon={() => abandon.mutate(cs.changeset_id)}
                    onRevert={() => revert.mutate(cs.changeset_id)}
                    onRetry={() => retry.mutate(cs.changeset_id)}
                  />
                ))}
              </TableBody>
            </Table>
          </CardContent>
        </Card>
      )}
    </div>
  )
}

interface RowProps {
  cs: Changeset
  busy: boolean
  onAbandon: () => void
  onRevert: () => void
  onRetry: () => void
}

function ChangesetRow({ cs, busy, onAbandon, onRevert, onRetry }: RowProps) {
  const hasPullRequest = cs.pr_number !== null || cs.pr_url !== null

  return (
    <TableRow>
      <TableCell className="font-medium">
        <Link to={`/codegen/${cs.changeset_id}`} className="hover:underline">
          {cs.task.title}
        </Link>
      </TableCell>
      <TableCell>
        <ChangesetStatusPill status={cs.status} />
      </TableCell>
      <TableCell>
        <div className="flex flex-wrap items-center gap-2">
          {cs.github_pr_status ? <GitHubPRStatusPill status={cs.github_pr_status} /> : null}
          {cs.pr_url ? (
            <a
              href={cs.pr_url}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 text-sm underline"
            >
              #{cs.pr_number}
              <ExternalLink className="h-3 w-3" />
            </a>
          ) : cs.pr_number !== null ? (
            <span className="text-sm">#{cs.pr_number}</span>
          ) : (
            '—'
          )}
          {cs.head_sha ? (
            <code className="font-mono text-xs text-muted-foreground">
              {cs.head_sha.slice(0, 12)}
            </code>
          ) : null}
        </div>
      </TableCell>
      <TableCell className="text-sm text-muted-foreground">
        {cs.external_ci_status ? (
          <div className="space-y-1">
            <ExternalCIStatusPill status={cs.external_ci_status} />
            {cs.external_ci_status === 'unverified_external_ci' ? (
              <p className="text-xs text-amber-700 dark:text-amber-400">no CI configured</p>
            ) : null}
          </div>
        ) : (
          '—'
        )}
      </TableCell>
      <TableCell>
        <RelativeTime value={cs.updated_at} />
      </TableCell>
      <TableCell className="space-x-2 text-right">
        {cs.status === 'merged' ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={onRevert}>
            Revert
          </Button>
        ) : hasPullRequest ? (
          cs.pr_url ? (
            <Button size="sm" asChild>
              <a href={cs.pr_url} target="_blank" rel="noreferrer">Open PR on GitHub</a>
            </Button>
          ) : null
        ) : cs.status === 'error' ? (
          <Button size="sm" variant="outline" disabled={busy} onClick={onRetry}>
            Retry
          </Button>
        ) : cs.status === 'queued' ? (
          <Button size="sm" variant="ghost" disabled={busy} onClick={onAbandon}>
            Abandon
          </Button>
        ) : null}
      </TableCell>
    </TableRow>
  )
}
