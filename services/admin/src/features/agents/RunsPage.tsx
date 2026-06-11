// Runs history (plan §5.6.4) — localStorage-tracked runs until the server
// grows a runs-list endpoint (gap G1).
import { Play } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { useWorkspace } from '@/core/workspace'
import { loadTrackedRuns } from '@/features/agents/runHistory'
import { RunStatusPill } from '@/features/agents/RunStatusPill'

export function RunsPage() {
  const { active } = useWorkspace()
  const navigate = useNavigate()
  const [runs] = useState(() => (active ? loadTrackedRuns(active.id) : []))

  return (
    <div className="space-y-4">
      <PageHeader
        title="Agent runs"
        description="Runs triggered from this browser — server-side run history is pending the runs-list endpoint (plan gap G1)."
        actions={
          <Button size="sm" asChild>
            <Link to="/agents/trigger">
              <Play />
              Trigger run
            </Link>
          </Button>
        }
      />

      <Card>
        <CardContent className="p-0">
          {runs.length === 0 ? (
            <EmptyState
              title="No runs tracked in this browser"
              description="Trigger a run to watch the autonomous loop analyze, design, and propose."
            >
              <Button size="sm" asChild>
                <Link to="/agents/trigger">
                  <Play />
                  Trigger run
                </Link>
              </Button>
            </EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Run</TableHead>
                  <TableHead>Last seen status</TableHead>
                  <TableHead>Autonomy</TableHead>
                  <TableHead>Agents</TableHead>
                  <TableHead>Triggered</TableHead>
                </TableRow>
              </TableHeader>
              <TableBody>
                {runs.map((run) => (
                  <TableRow
                    key={run.run_id}
                    className="cursor-pointer"
                    onClick={() => navigate(`/agents/runs/${encodeURIComponent(run.run_id)}`)}
                  >
                    <TableCell>
                      <code className="font-mono text-xs">{run.run_id.slice(0, 8)}…</code>
                    </TableCell>
                    <TableCell>
                      <RunStatusPill status={run.last_status} />
                    </TableCell>
                    <TableCell className="tabular-nums">L{run.autonomy_level}</TableCell>
                    <TableCell className="text-xs text-muted-foreground">
                      {run.analysis_types.map((type) => type.replace(/_/g, ' ')).join(', ')}
                    </TableCell>
                    <TableCell>
                      <RelativeTime value={run.triggered_at} className="text-muted-foreground" />
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>
    </div>
  )
}
