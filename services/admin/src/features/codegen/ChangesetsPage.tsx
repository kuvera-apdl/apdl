// Codegen changesets: the autonomous PRs the loop opens on connected repos.
// Lists changesets for the active workspace's project and exposes the gated
// actions (merge on green CI, abandon un-merged, revert a merged change).
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink } from 'lucide-react'
import { Link } from 'react-router-dom'
import { toast } from 'sonner'

import { abandonChangeset, listChangesets, mergeChangeset, revertChangeset } from '@/api/codegen'
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
import { projectIdFromKey, serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'
import { ChangesetStatusPill } from '@/features/codegen/ChangesetStatusPill'

const REFETCH_MS = 5000
const TERMINAL = new Set(['merged', 'abandoned', 'tests_failed', 'error'])

export function ChangesetsPage() {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  // This route is behind RequireWorkspace, so `active` is non-null when rendered.
  const ws = active as Workspace
  const projectId = active ? (projectIdFromKey(active.apiKey) ?? '') : ''

  const query = useQuery({
    queryKey: active ? queryKeys.changesets(active.id) : ['none', 'changesets'],
    enabled: active !== null && projectId !== '',
    refetchInterval: REFETCH_MS,
    queryFn: () =>
      listChangesets(serviceConnection(ws, 'codegen'), ws.internalToken, { projectId }),
  })

  const invalidate = () => {
    if (active) void queryClient.invalidateQueries({ queryKey: queryKeys.changesets(active.id) })
  }
  const onError = (fallback: string) => (error: Error) =>
    toast.error(error instanceof ApiError ? error.message : fallback)

  const merge = useMutation({
    mutationFn: (id: string) =>
      mergeChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Merge requested')
      invalidate()
    },
    onError: onError('Merge failed'),
  })
  const abandon = useMutation({
    mutationFn: (id: string) =>
      abandonChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Changeset abandoned')
      invalidate()
    },
    onError: onError('Abandon failed'),
  })
  const revert = useMutation({
    mutationFn: (id: string) =>
      revertChangeset(serviceConnection(ws, 'codegen'), ws.internalToken, id),
    onSuccess: () => {
      toast.success('Revert PR requested')
      invalidate()
    },
    onError: onError('Revert failed'),
  })
  const busy = merge.isPending || abandon.isPending || revert.isPending

  return (
    <div className="space-y-6">
      <PageHeader
        title="Code changes"
        description="Autonomous changesets — branches, draft PRs, and merges on connected repos. Polled every 5s."
      />
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
                  <TableHead>Status</TableHead>
                  <TableHead>CI</TableHead>
                  <TableHead>PR</TableHead>
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
                    onMerge={() => merge.mutate(cs.changeset_id)}
                    onAbandon={() => abandon.mutate(cs.changeset_id)}
                    onRevert={() => revert.mutate(cs.changeset_id)}
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
  onMerge: () => void
  onAbandon: () => void
  onRevert: () => void
}

function ChangesetRow({ cs, busy, onMerge, onAbandon, onRevert }: RowProps) {
  // 'passed' = CI green; 'none' = the repo has no CI configured, so there is no
  // gate to clear and merge is allowed. Anything else (pending/failed) blocks.
  const mergeable =
    (cs.status === 'ci_passed' || cs.status === 'waiting_approval') &&
    (cs.ci_status === 'passed' || cs.ci_status === 'none')

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
      <TableCell className="text-sm text-muted-foreground">
        {cs.ci_status === 'none' ? 'no CI' : (cs.ci_status ?? '—')}
      </TableCell>
      <TableCell>
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
        ) : (
          <>
            <Button size="sm" disabled={busy || !mergeable} onClick={onMerge}>
              Merge
            </Button>
            <Button
              size="sm"
              variant="ghost"
              disabled={busy || TERMINAL.has(cs.status)}
              onClick={onAbandon}
            >
              Abandon
            </Button>
          </>
        )}
      </TableCell>
    </TableRow>
  )
}
