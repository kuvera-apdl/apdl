// Runs history (plan §5.6.4, upgraded by gap G1): the server-side run list
// with status filtering.
import { useQuery } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { listRuns, listRunsCurl } from '@/api/agents'
import { ApiError } from '@/api/http'
import { KNOWN_RUN_STATUSES, TERMINAL_RUN_STATUSES } from '@/api/schemas/agents'
import type { RunsListResponse } from '@/api/types/agents'
import { CurlButton } from '@/components/shared/CurlButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'
import { AgentReadOnlyNote } from '@/features/agents/AgentAccessNotice'
import { RunStatusPill } from '@/features/agents/RunStatusPill'

export function RunsPage() {
  const { active, projectId } = useWorkspace()
  const navigate = useNavigate()
  const [statusFilter, setStatusFilter] = useState('')
  const canRun = hasWorkspaceRole(active, 'agents:run')

  const runsQuery = useQuery<RunsListResponse>({
    queryKey: [active?.id ?? 'none', 'agent-runs-list', statusFilter],
    enabled: active !== null && projectId !== null,
    refetchInterval: (query) =>
      query.state.data?.runs.some((run) => !TERMINAL_RUN_STATUSES.has(run.status)) ? 10_000 : false,
    queryFn: ({ signal }) =>
      listRuns(
        serviceConnection(active!, 'agents'),
        { projectId: projectId!, ...(statusFilter ? { status: statusFilter } : {}) },
        { signal },
      ),
  })

  const endpointMissing = runsQuery.error instanceof ApiError && runsQuery.error.status === 404
  const conn = active ? serviceConnection(active, 'agents') : null

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agent runs"
        description={
          runsQuery.data ? `${runsQuery.data.count} runs for this project` : 'Server-side run history.'
        }
        actions={
          <>
            {conn && projectId ? (
              <CurlButton
                spec={listRunsCurl(conn, { projectId, ...(statusFilter ? { status: statusFilter } : {}) })}
                title="List runs"
              />
            ) : null}
            {canRun ? (
              <Button size="sm" asChild>
                <Link to="/agents/trigger">
                  <Play />
                  Trigger run
                </Link>
              </Button>
            ) : null}
          </>
        }
      />

      {!canRun ? (
        <AgentReadOnlyNote>
          Run history is read-only. Starting a run requires agents:run on an
          operator-provisioned workspace.
        </AgentReadOnlyNote>
      ) : null}

      {endpointMissing ? (
        <EmptyState
          title="Runs list unavailable"
          description="This agents service predates the runs-list endpoint — upgrade it to browse run history."
        />
      ) : (
        <>
          <div className="flex items-end gap-3">
            <div className="space-y-1.5">
              <Label>Status</Label>
              <Select
                value={statusFilter}
                onChange={(event) => setStatusFilter(event.target.value)}
                className="w-52"
                aria-label="Status filter"
              >
                <option value="">all statuses</option>
                {KNOWN_RUN_STATUSES.map((status) => (
                  <option key={status} value={status}>
                    {status}
                  </option>
                ))}
              </Select>
            </div>
          </div>

          {runsQuery.isPending ? <Skeleton className="h-48 w-full" /> : null}
          {runsQuery.error && !endpointMissing ? (
            <ErrorState error={runsQuery.error} onRetry={() => void runsQuery.refetch()} />
          ) : null}
          {runsQuery.data ? (
            <Card>
              <CardContent className="p-0">
                {runsQuery.data.runs.length === 0 ? (
                  <EmptyState
                    title="No runs yet"
                    description={
                      canRun
                        ? 'Trigger a run to watch the autonomous loop analyze, design, and propose.'
                        : 'No run history exists for this read-only workspace.'
                    }
                  >
                    {canRun ? (
                      <Button size="sm" asChild>
                        <Link to="/agents/trigger">
                          <Play />
                          Trigger run
                        </Link>
                      </Button>
                    ) : null}
                  </EmptyState>
                ) : (
                  <Table>
                    <TableHeader>
                      <TableRow>
                        <TableHead>Run</TableHead>
                        <TableHead>Status</TableHead>
                        <TableHead>Phase</TableHead>
                        <TableHead className="text-right">Insights</TableHead>
                        <TableHead className="text-right">Designs</TableHead>
                        <TableHead>Started</TableHead>
                      </TableRow>
                    </TableHeader>
                    <TableBody>
                      {runsQuery.data.runs.map((run) => (
                        <TableRow
                          key={run.run_id}
                          className="cursor-pointer"
                          onClick={() => navigate(`/agents/runs/${encodeURIComponent(run.run_id)}`)}
                        >
                          <TableCell>
                            <code className="font-mono text-xs">{run.run_id.slice(0, 8)}…</code>
                          </TableCell>
                          <TableCell>
                            <RunStatusPill status={run.status} />
                          </TableCell>
                          <TableCell>
                            <code className="font-mono text-xs text-muted-foreground">{run.phase}</code>
                          </TableCell>
                          <TableCell className="text-right tabular-nums">{run.insights_count}</TableCell>
                          <TableCell className="text-right tabular-nums">{run.experiments_count}</TableCell>
                          <TableCell>
                            <RelativeTime value={run.started_at} className="text-muted-foreground" />
                          </TableCell>
                        </TableRow>
                      ))}
                    </TableBody>
                  </Table>
                )}
              </CardContent>
            </Card>
          ) : null}
        </>
      )}
    </div>
  )
}
