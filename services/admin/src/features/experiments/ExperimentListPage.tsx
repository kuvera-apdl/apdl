import { createColumnHelper, type ColumnDef } from '@tanstack/react-table'
import { Plus } from 'lucide-react'
import { useMemo } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { listExperimentsCurl } from '@/api/experiments'
import type { ExperimentEntry } from '@/api/types/experiments'
import { CurlButton } from '@/components/shared/CurlButton'
import { DataTable } from '@/components/shared/DataTable'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StatePill } from '@/components/shared/StatePill'
import { Button } from '@/components/ui/button'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'
import { useExperimentsQuery } from '@/features/experiments/hooks'
import { ExperimentStatusPill } from '@/features/experiments/StatusPill'
import { formatPercent } from '@/lib/format'

const columnHelper = createColumnHelper<ExperimentEntry>()

export function ExperimentListPage() {
  const { active } = useWorkspace()
  const canWrite = hasWorkspaceRole(active, 'config:write')
  const navigate = useNavigate()
  const experimentsQuery = useExperimentsQuery()
  const conn = active ? serviceConnection(active, 'config') : null

  const columns = useMemo<ColumnDef<ExperimentEntry, unknown>[]>(
    () =>
      [
        columnHelper.accessor('key', {
          header: 'Key',
          cell: (info) => <code className="font-mono text-xs">{info.getValue()}</code>,
        }),
        columnHelper.accessor('status', {
          header: 'Status',
          cell: (info) =>
            info.row.original.archived_at ? (
              <StatePill state="archived" />
            ) : (
              <ExperimentStatusPill status={info.getValue()} />
            ),
        }),
        columnHelper.accessor('traffic_percentage', {
          header: 'Traffic',
          cell: (info) => <span className="tabular-nums">{formatPercent(info.getValue())}</span>,
        }),
        columnHelper.accessor('start_date', {
          header: 'Start',
          cell: (info) => info.getValue() || <span className="text-muted-foreground">—</span>,
        }),
        columnHelper.accessor('end_date', {
          header: 'End',
          cell: (info) => info.getValue() || <span className="text-muted-foreground">—</span>,
        }),
        columnHelper.accessor('updated_at', {
          header: 'Updated',
          cell: (info) => <RelativeTime value={info.getValue()} className="text-muted-foreground" />,
        }),
      ] as ColumnDef<ExperimentEntry, unknown>[],
    [],
  )

  return (
    <div className="space-y-4">
      <PageHeader
        title="Experiments"
        description={
          experimentsQuery.data ? `${experimentsQuery.data.count} experiments` : undefined
        }
        actions={
          <>
            {conn ? <CurlButton spec={listExperimentsCurl(conn)} title="List experiments" /> : null}
            {canWrite ? (
              <Button size="sm" asChild>
                <Link to="/experiments/new">
                  <Plus />
                  New experiment
                </Link>
              </Button>
            ) : null}
          </>
        }
      />
      <DataTable
        columns={columns}
        data={experimentsQuery.data?.experiments}
        isLoading={experimentsQuery.isPending}
        error={experimentsQuery.error}
        onRetry={() => void experimentsQuery.refetch()}
        onRowClick={(experiment) => navigate(`/experiments/${encodeURIComponent(experiment.key)}`)}
        rowClassName={(experiment) => (experiment.archived_at ? 'opacity-60' : undefined)}
        emptyState={
          <EmptyState
            title="No experiments yet"
            description={
              canWrite
                ? "Results use each experiment's configured backing flag, metric, and analysis window."
                : 'No experiments are available to this read-only workspace.'
            }
          >
            {canWrite ? (
              <Button size="sm" asChild>
                <Link to="/experiments/new">
                  <Plus />
                  New experiment
                </Link>
              </Button>
            ) : null}
          </EmptyState>
        }
      />
    </div>
  )
}
