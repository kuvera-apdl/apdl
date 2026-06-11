import { createColumnHelper, type ColumnDef } from '@tanstack/react-table'
import { MoreHorizontal, Plus, Search } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import { listFlagsCurl } from '@/api/config'
import type { FlagConfig, FlagState } from '@/api/types/flags'
import { CopyButton } from '@/components/shared/CopyButton'
import { CurlButton } from '@/components/shared/CurlButton'
import { DataTable } from '@/components/shared/DataTable'
import { OwnerBadges } from '@/components/shared/OwnerBadges'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StatePill } from '@/components/shared/StatePill'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Switch } from '@/components/ui/switch'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useFlagsQuery } from '@/features/flags/hooks'
import { LifecycleDialog, type LifecycleAction } from '@/features/flags/LifecycleDialog'
import { formatPercent, isPastDate, parseServerDate, variantSummary } from '@/lib/format'
import { cn } from '@/lib/utils'

const FLAG_STATES: FlagState[] = ['draft', 'active', 'disabled', 'archived']

const columnHelper = createColumnHelper<FlagConfig>()

interface RowMenuProps {
  flag: FlagConfig
  onViewAudit: () => void
  onEdit: () => void
  onLifecycle: (action: LifecycleAction) => void
}

function RowMenu({ flag, onViewAudit, onEdit, onLifecycle }: RowMenuProps) {
  const copy = async (value: string, what: string) => {
    try {
      await navigator.clipboard.writeText(value)
      toast.success(`${what} copied`)
    } catch {
      toast.error('Clipboard unavailable')
    }
  }
  const archived = flag.state === 'archived'
  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button
          variant="ghost"
          size="icon"
          className="h-7 w-7"
          aria-label={`Actions for ${flag.key}`}
          onClick={(event) => event.stopPropagation()}
        >
          <MoreHorizontal className="h-4 w-4" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" onClick={(event) => event.stopPropagation()}>
        {!archived ? <DropdownMenuItem onSelect={onEdit}>Edit</DropdownMenuItem> : null}
        {flag.state === 'active' ? (
          <DropdownMenuItem onSelect={() => onLifecycle('disable')}>Disable…</DropdownMenuItem>
        ) : null}
        {!archived ? (
          <DropdownMenuItem onSelect={() => onLifecycle('archive')}>Archive…</DropdownMenuItem>
        ) : null}
        <DropdownMenuItem onSelect={() => void copy(flag.key, 'Key')}>Copy key</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void copy(JSON.stringify(flag, null, 2), 'Flag JSON')}>
          Copy flag JSON
        </DropdownMenuItem>
        <DropdownMenuItem onSelect={onViewAudit}>View audit trail</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

export function FlagListPage() {
  const { active } = useWorkspace()
  const navigate = useNavigate()
  const [searchParams, setSearchParams] = useSearchParams()
  const flagsQuery = useFlagsQuery()
  const [lifecycle, setLifecycle] = useState<{ flag: FlagConfig; action: LifecycleAction } | null>(null)

  const search = searchParams.get('q') ?? ''
  const showArchived = searchParams.get('archived') === '1'
  const stateFilter = useMemo(() => {
    const raw = searchParams.get('state')
    if (!raw) return new Set<FlagState>()
    return new Set(raw.split(',').filter((s): s is FlagState => FLAG_STATES.includes(s as FlagState)))
  }, [searchParams])

  const updateParams = (updates: Record<string, string | null>) => {
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        for (const [key, value] of Object.entries(updates)) {
          if (value === null || value === '') next.delete(key)
          else next.set(key, value)
        }
        return next
      },
      { replace: true },
    )
  }

  const toggleState = (state: FlagState) => {
    const next = new Set(stateFilter)
    if (next.has(state)) next.delete(state)
    else next.add(state)
    updateParams({ state: next.size > 0 ? [...next].join(',') : null })
  }

  const allFlags = flagsQuery.data?.flags
  const filtered = useMemo(() => {
    if (!allFlags) return undefined
    const query = search.trim().toLowerCase()
    return allFlags.filter((flag) => {
      if (!showArchived && flag.state === 'archived' && !stateFilter.has('archived')) return false
      if (stateFilter.size > 0 && !stateFilter.has(flag.state)) return false
      if (
        query &&
        ![flag.key, flag.name, flag.description].some((field) =>
          field.toLowerCase().includes(query),
        )
      ) {
        return false
      }
      return true
    })
  }, [allFlags, search, showArchived, stateFilter])

  const openDetail = (flag: FlagConfig, tab?: string) => {
    navigate(`/flags/${encodeURIComponent(flag.key)}${tab ? `?tab=${tab}` : ''}`)
  }

  const columns = useMemo<ColumnDef<FlagConfig, unknown>[]>(
    () =>
      [
        columnHelper.accessor('key', {
          header: 'Key',
          cell: (info) => (
            <span className="flex items-center gap-1">
              <code className="font-mono text-xs">{info.getValue()}</code>
              <CopyButton value={info.getValue()} label="Copy key" />
            </span>
          ),
        }),
        columnHelper.accessor('name', { header: 'Name' }),
        columnHelper.accessor('state', {
          header: 'State',
          cell: (info) => <StatePill state={info.getValue()} />,
        }),
        columnHelper.accessor('default_variant', {
          header: 'Default',
          cell: (info) => <code className="font-mono text-xs">{info.getValue()}</code>,
        }),
        columnHelper.display({
          id: 'variants',
          header: 'Variants',
          cell: ({ row }) => (
            <span className="text-xs text-muted-foreground">
              {variantSummary(row.original.variants)}
            </span>
          ),
        }),
        columnHelper.accessor((flag) => flag.fallthrough.rollout.percentage, {
          id: 'fallthrough',
          header: 'Fallthrough',
          cell: (info) => formatPercent(info.getValue()),
        }),
        columnHelper.accessor((flag) => flag.rules.length, {
          id: 'rules',
          header: 'Rules',
        }),
        columnHelper.display({
          id: 'owners',
          header: 'Owners',
          cell: ({ row }) => <OwnerBadges owners={row.original.owners} />,
        }),
        columnHelper.accessor('review_by', {
          header: 'Review by',
          cell: (info) => {
            const value = info.getValue()
            if (!value) return <span className="text-muted-foreground">—</span>
            const overdue = isPastDate(value) && info.row.original.state !== 'archived'
            return (
              <span className={cn('tabular-nums', overdue && 'font-medium text-destructive')}>
                {value}
              </span>
            )
          },
        }),
        columnHelper.accessor('version', {
          header: 'Version',
          cell: (info) => <span className="tabular-nums">v{info.getValue()}</span>,
        }),
        columnHelper.accessor((flag) => parseServerDate(flag.updated_at)?.getTime() ?? 0, {
          id: 'updated_at',
          header: 'Updated',
          cell: ({ row }) => (
            <RelativeTime value={row.original.updated_at} className="text-muted-foreground" />
          ),
        }),
        columnHelper.display({
          id: 'actions',
          cell: ({ row }) => (
            <RowMenu
              flag={row.original}
              onViewAudit={() => openDetail(row.original, 'audit')}
              onEdit={() => navigate(`/flags/${encodeURIComponent(row.original.key)}/edit`)}
              onLifecycle={(action) => setLifecycle({ flag: row.original, action })}
            />
          ),
        }),
      ] as ColumnDef<FlagConfig, unknown>[],
    // openDetail only captures react-router's stable navigate.
    [],
  )

  const conn = active ? serviceConnection(active, 'config') : null

  return (
    <div className="space-y-4">
      <PageHeader
        title="Feature flags"
        description={
          filtered !== undefined && allFlags !== undefined
            ? `${filtered.length} of ${allFlags.length} flags`
            : undefined
        }
        actions={
          <>
            {conn ? <CurlButton spec={listFlagsCurl(conn, showArchived)} title="List flags" /> : null}
            <Button size="sm" asChild>
              <Link to="/flags/new">
                <Plus />
                New flag
              </Link>
            </Button>
          </>
        }
      />

      {lifecycle ? (
        <LifecycleDialog
          flag={lifecycle.flag}
          action={lifecycle.action}
          onClose={() => setLifecycle(null)}
        />
      ) : null}

      <div className="flex flex-wrap items-center gap-3">
        <div className="relative">
          <Search className="absolute left-2.5 top-1/2 h-4 w-4 -translate-y-1/2 text-muted-foreground" />
          <Input
            value={search}
            onChange={(event) => updateParams({ q: event.target.value })}
            placeholder="Search key, name, description…"
            className="w-72 pl-8"
          />
        </div>
        <div className="flex items-center gap-1.5">
          {FLAG_STATES.map((state) => (
            <button
              key={state}
              type="button"
              onClick={() => toggleState(state)}
              className={cn(
                'rounded-full border px-2.5 py-1 text-xs font-medium transition-colors',
                stateFilter.has(state)
                  ? 'border-foreground bg-foreground text-background'
                  : 'text-muted-foreground hover:bg-accent',
              )}
              aria-pressed={stateFilter.has(state)}
            >
              {state}
            </button>
          ))}
        </div>
        <div className="flex items-center gap-2">
          <Switch
            id="show-archived"
            checked={showArchived}
            onCheckedChange={(checked) => updateParams({ archived: checked ? '1' : null })}
          />
          <Label htmlFor="show-archived" className="text-muted-foreground">
            Show archived
          </Label>
        </div>
      </div>

      <DataTable
        columns={columns}
        data={filtered}
        isLoading={flagsQuery.isPending}
        error={flagsQuery.error}
        onRetry={() => void flagsQuery.refetch()}
        onRowClick={(flag) => openDetail(flag)}
        initialSorting={[{ id: 'updated_at', desc: true }]}
        rowClassName={(flag) => (flag.state === 'archived' ? 'opacity-60' : undefined)}
        emptyState={
          allFlags !== undefined && allFlags.length === 0 ? (
            <EmptyState title="No flags yet" description="Create your first flag to start the Loop.">
              <Button size="sm" asChild>
                <Link to="/flags/new">
                  <Plus />
                  New flag
                </Link>
              </Button>
            </EmptyState>
          ) : (
            'No flags match the current filters.'
          )
        }
      />
    </div>
  )
}
