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
import { Button } from '@/components/ui/button'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useExperimentsQuery } from '@/features/experiments/hooks'
import { ExperimentStatusPill } from '@/features/experiments/StatusPill'
import { formatPercent } from '@/lib/format'

const columnHelper = createColumnHelper<ExperimentEntry>()

export function ExperimentListPage() {
  const { active } = useWorkspace()
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
          cell: (info) => <ExperimentStatusPill status={info.getValue()} />,
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
            <Button size="sm" asChild>
              <Link to="/experiments/new">
                <Plus />
                New experiment
              </Link>
            </Button>
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
        emptyState={
          <EmptyState
            title="No experiments yet"
            description="An experiment is measured through a flag's exposures — create one and link a flag on its Results tab."
          >
            <Button size="sm" asChild>
              <Link to="/experiments/new">
                <Plus />
                New experiment
              </Link>
            </Button>
          </EmptyState>
        }
      />
    </div>
  )
}
