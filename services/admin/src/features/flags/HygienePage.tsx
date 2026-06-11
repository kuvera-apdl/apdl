// Flag hygiene (plan §5.3.5): the stale-flags report, the periodic-review
// surface behind review_by.
import { createColumnHelper, type ColumnDef } from '@tanstack/react-table'
import { useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { staleFlagsCurl } from '@/api/config'
import type { StaleFlag, StaleReason } from '@/api/types/flags'
import { CurlButton } from '@/components/shared/CurlButton'
import { DataTable } from '@/components/shared/DataTable'
import { PageHeader } from '@/components/shared/PageHeader'
import { StatePill } from '@/components/shared/StatePill'
import { Badge } from '@/components/ui/badge'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useStaleFlagsQuery } from '@/features/flags/hooks'
import { useDebouncedValue } from '@/lib/hooks'

const REASON_DEFINITIONS: Record<StaleReason, { label: string; definition: string }> = {
  missing_owner: { label: 'no owner', definition: 'The flag lists no owners.' },
  missing_review_date: { label: 'no review date', definition: 'No review_by date is set.' },
  review_overdue: { label: 'review overdue', definition: 'The review_by date is in the past.' },
  stale_draft: {
    label: 'stale draft',
    definition: 'Draft unchanged for longer than the selected window.',
  },
  stale_disabled: {
    label: 'stale disabled',
    definition: 'Disabled unchanged for longer than the selected window.',
  },
  fully_rolled_out: {
    label: 'fully rolled out',
    definition:
      'Active at 100% fallthrough with a single winning variant — eligible for cleanup archive.',
  },
}

const columnHelper = createColumnHelper<StaleFlag>()

export function HygienePage() {
  const { active } = useWorkspace()
  const navigate = useNavigate()
  const [olderThanDays, setOlderThanDays] = useState(90)
  const debouncedDays = useDebouncedValue(olderThanDays)
  const staleQuery = useStaleFlagsQuery(debouncedDays)

  const columns = useMemo<ColumnDef<StaleFlag, unknown>[]>(
    () =>
      [
        columnHelper.accessor('key', {
          header: 'Key',
          cell: (info) => <code className="font-mono text-xs">{info.getValue()}</code>,
        }),
        columnHelper.accessor('state', {
          header: 'State',
          cell: (info) => <StatePill state={info.getValue()} />,
        }),
        columnHelper.accessor('days_since_update', {
          header: 'Days stale',
          cell: (info) => <span className="tabular-nums">{info.getValue()}</span>,
        }),
        columnHelper.display({
          id: 'stale_reasons',
          header: 'Reasons',
          cell: ({ row }) => (
            <span className="flex flex-wrap gap-1">
              {row.original.stale_reasons.map((reason) => (
                <Tooltip key={reason}>
                  <TooltipTrigger asChild>
                    <span className="cursor-default rounded-full border bg-secondary px-2 py-0.5 text-xs">
                      {REASON_DEFINITIONS[reason].label}
                    </span>
                  </TooltipTrigger>
                  <TooltipContent>{REASON_DEFINITIONS[reason].definition}</TooltipContent>
                </Tooltip>
              ))}
            </span>
          ),
        }),
        columnHelper.accessor('cleanup_recommended', {
          header: 'Cleanup',
          cell: (info) =>
            info.getValue() ? (
              <Badge>recommended</Badge>
            ) : (
              <span className="text-muted-foreground">—</span>
            ),
        }),
      ] as ColumnDef<StaleFlag, unknown>[],
    [],
  )

  const conn = active ? serviceConnection(active, 'config') : null
  const count = staleQuery.data?.count

  return (
    <div className="space-y-4">
      <PageHeader
        title="Flag hygiene"
        description={
          count !== undefined
            ? `${count} flag${count === 1 ? '' : 's'} need attention`
            : 'Flags that need owner review or rollout cleanup.'
        }
        actions={conn ? <CurlButton spec={staleFlagsCurl(conn, debouncedDays)} title="Stale flags" /> : null}
      />

      <div className="flex items-center gap-3">
        <Label htmlFor="older-than" className="text-muted-foreground">
          Staleness window
        </Label>
        <input
          id="older-than"
          type="range"
          min={7}
          max={365}
          value={olderThanDays}
          onChange={(event) => setOlderThanDays(Number(event.target.value))}
          className="w-56 accent-foreground"
        />
        <Input
          type="number"
          min={7}
          max={365}
          value={olderThanDays}
          onChange={(event) => {
            const value = Number(event.target.value)
            if (Number.isFinite(value)) setOlderThanDays(Math.min(365, Math.max(7, value)))
          }}
          className="w-20 tabular-nums"
          aria-label="Older than days"
        />
        <span className="text-sm text-muted-foreground">days</span>
      </div>

      <DataTable
        columns={columns}
        data={staleQuery.data?.flags}
        isLoading={staleQuery.isPending}
        error={staleQuery.error}
        onRetry={() => void staleQuery.refetch()}
        onRowClick={(flag) => navigate(`/flags/${encodeURIComponent(flag.key)}`)}
        initialSorting={[{ id: 'days_since_update', desc: true }]}
        emptyState="Nothing stale — every flag has owners, review dates, and recent activity."
      />

      <p className="text-xs text-muted-foreground">
        The cleanup action (archive a fully-rolled-out flag) ships with the flag-write phase; until
        then use the API's <code className="font-mono">POST /v1/admin/flags/{'{key}'}/cleanup</code>.
      </p>
    </div>
  )
}
