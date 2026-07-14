// Watch — the loop's live state (admin-console-purpose-ia.md). A stage board
// of what's moving through the loop right now. Composed from the runs list
// today (grouped by loop stage); a future GET /v1/agents/loop/threads will
// enrich each card into a full experiment thread.
import { useQuery } from '@tanstack/react-query'
import { Play } from 'lucide-react'
import { Link, useNavigate } from 'react-router-dom'

import { listRuns } from '@/api/agents'
import { ApiError } from '@/api/http'
import type { RunStatus } from '@/api/types/agents'
import { LoopStatusPill } from '@/components/shared/LoopStatusPill'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StageBoard, type BoardColumn } from '@/components/shared/StageBoard'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { runToLoopStage, type LoopStage } from '@/lib/loopStatus'

// Which loop stages fall in each board column.
const COLUMNS: { key: string; title: string; stages: LoopStage[] }[] = [
  { key: 'active', title: 'Working', stages: ['designing'] },
  { key: 'decide', title: 'Awaiting you', stages: ['awaiting_approval'] },
  { key: 'building', title: 'Building', stages: ['building'] },
  { key: 'done', title: 'Recently done', stages: ['done', 'ship', 'rollback', 'iterate', 'failed'] },
]

export function WatchPage() {
  const { active, projectId } = useWorkspace()
  const navigate = useNavigate()
  const conn = active ? serviceConnection(active, 'agents') : null

  const runsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'watch', 'runs'],
    enabled: Boolean(conn && projectId),
    refetchInterval: 10_000,
    queryFn: ({ signal }) => listRuns(conn!, { projectId: projectId!, limit: 50 }, { signal }),
  })

  const endpointMissing = runsQuery.error instanceof ApiError && runsQuery.error.status === 404
  const runs = runsQuery.data?.runs ?? []

  const staged = runs.map((run) => ({ run, stage: runToLoopStage(run.status, run.phase) }))
  const active_ = staged.filter((s) => !['done', 'ship', 'rollback', 'iterate', 'failed'].includes(s.stage))
  const columns: BoardColumn<{ run: RunStatus; stage: LoopStage }>[] = COLUMNS.map((col) => ({
    key: col.key,
    title: col.title,
    items: staged.filter((s) => col.stages.includes(s.stage)),
  }))

  const pulse =
    active_.length === 0
      ? 'Idle — nothing running'
      : `${active_.length} active · ${columns[1]!.items.length} awaiting you`

  return (
    <div className="space-y-4">
      <PageHeader
        title="Watch"
        description={runsQuery.data ? pulse : 'What the loop is doing right now.'}
        actions={
          <Button size="sm" asChild>
            <Link to="/agents/trigger">
              <Play />
              Run loop
            </Link>
          </Button>
        }
      />

      {runsQuery.isPending ? (
        <div className="grid gap-3 md:grid-cols-2 xl:grid-cols-4">
          {COLUMNS.map((c) => (
            <div key={c.key} className="h-32 animate-pulse rounded-lg border border-dashed" />
          ))}
        </div>
      ) : null}

      {runsQuery.error && !endpointMissing ? (
        <ErrorState error={runsQuery.error} onRetry={() => void runsQuery.refetch()} />
      ) : null}
      {endpointMissing ? (
        <EmptyState
          title="Agents service too old"
          description="This agents service predates the runs-list endpoint, so the board can't be assembled."
        />
      ) : null}

      {runsQuery.data && runs.length === 0 ? (
        <EmptyState
          title="The loop hasn't run yet"
          description="Run the loop to watch it analyze, design, build, and measure."
        >
          <Button size="sm" asChild>
            <Link to="/agents/trigger">
              <Play />
              Run loop
            </Link>
          </Button>
        </EmptyState>
      ) : null}

      {runsQuery.data && runs.length > 0 ? (
        <StageBoard
          columns={columns}
          emptyLabel="nothing here"
          renderItem={({ run, stage }) => (
            <Card
              key={run.run_id}
              className="cursor-pointer transition-colors hover:border-foreground/20"
              onClick={() => navigate(`/agents/runs/${encodeURIComponent(run.run_id)}`)}
            >
              <CardContent className="space-y-1.5 p-3">
                <div className="flex items-center justify-between gap-2">
                  <code className="font-mono text-xs">{run.run_id.slice(0, 8)}…</code>
                  <LoopStatusPill stage={stage} pulse={stage === 'awaiting_approval'} />
                </div>
                <p className="truncate text-xs text-muted-foreground">
                  {run.phase.replace(/_/g, ' ')}
                </p>
                <div className="flex items-center justify-between text-xs text-muted-foreground">
                  <span className="tabular-nums">
                    {run.insights_count}i · {run.experiments_count}x
                  </span>
                  <RelativeTime value={run.updated_at} />
                </div>
              </CardContent>
            </Card>
          )}
        />
      ) : null}
    </div>
  )
}
