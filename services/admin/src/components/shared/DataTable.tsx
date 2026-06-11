// Shared table wrapper (plan §4.6): TanStack Table with column sort, sticky
// header, and the shared loading/empty/error triplet every list view uses.
import {
  flexRender,
  getCoreRowModel,
  getSortedRowModel,
  useReactTable,
  type ColumnDef,
  type SortingState,
} from '@tanstack/react-table'
import { ArrowDown, ArrowUp, ArrowUpDown } from 'lucide-react'
import { useState, type ReactNode } from 'react'

import { ErrorState } from '@/components/shared/PanelStates'
import { Skeleton } from '@/components/ui/skeleton'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'
import { cn } from '@/lib/utils'

interface DataTableProps<TData> {
  columns: ColumnDef<TData, unknown>[]
  data: TData[] | undefined
  isLoading?: boolean
  error?: Error | null
  onRetry?: () => void
  onRowClick?: (row: TData) => void
  emptyState?: ReactNode
  initialSorting?: SortingState
  rowClassName?: (row: TData) => string | undefined
}

export function DataTable<TData>({
  columns,
  data,
  isLoading = false,
  error = null,
  onRetry,
  onRowClick,
  emptyState,
  initialSorting = [],
  rowClassName,
}: DataTableProps<TData>) {
  const [sorting, setSorting] = useState<SortingState>(initialSorting)

  const table = useReactTable({
    data: data ?? [],
    columns,
    state: { sorting },
    onSortingChange: setSorting,
    getCoreRowModel: getCoreRowModel(),
    getSortedRowModel: getSortedRowModel(),
  })

  if (error) {
    return (
      <div className="rounded-lg border">
        <ErrorState error={error} onRetry={onRetry} />
      </div>
    )
  }

  const rows = table.getRowModel().rows

  return (
    <div className="relative max-h-[75vh] overflow-auto rounded-lg border">
      <Table>
        <TableHeader className="sticky top-0 z-10 bg-background shadow-[inset_0_-1px_0_hsl(var(--border))]">
          {table.getHeaderGroups().map((headerGroup) => (
            <TableRow key={headerGroup.id} className="hover:bg-transparent">
              {headerGroup.headers.map((header) => {
                const sortable = header.column.getCanSort()
                const direction = header.column.getIsSorted()
                return (
                  <TableHead key={header.id}>
                    {header.isPlaceholder ? null : sortable ? (
                      <button
                        type="button"
                        className="inline-flex items-center gap-1 hover:text-foreground"
                        onClick={header.column.getToggleSortingHandler()}
                      >
                        {flexRender(header.column.columnDef.header, header.getContext())}
                        {direction === 'asc' ? (
                          <ArrowUp className="h-3.5 w-3.5" />
                        ) : direction === 'desc' ? (
                          <ArrowDown className="h-3.5 w-3.5" />
                        ) : (
                          <ArrowUpDown className="h-3.5 w-3.5 opacity-40" />
                        )}
                      </button>
                    ) : (
                      flexRender(header.column.columnDef.header, header.getContext())
                    )}
                  </TableHead>
                )
              })}
            </TableRow>
          ))}
        </TableHeader>
        <TableBody>
          {isLoading ? (
            Array.from({ length: 5 }).map((_, rowIndex) => (
              <TableRow key={rowIndex}>
                {columns.map((_, columnIndex) => (
                  <TableCell key={columnIndex}>
                    <Skeleton className="h-4 w-full max-w-32" />
                  </TableCell>
                ))}
              </TableRow>
            ))
          ) : rows.length === 0 ? (
            <TableRow className="hover:bg-transparent">
              <TableCell colSpan={columns.length} className="h-32 text-center text-muted-foreground">
                {emptyState ?? 'No results.'}
              </TableCell>
            </TableRow>
          ) : (
            rows.map((row) => (
              <TableRow
                key={row.id}
                className={cn(onRowClick && 'cursor-pointer', rowClassName?.(row.original))}
                onClick={onRowClick ? () => onRowClick(row.original) : undefined}
              >
                {row.getVisibleCells().map((cell) => (
                  <TableCell key={cell.id}>
                    {flexRender(cell.column.columnDef.cell, cell.getContext())}
                  </TableCell>
                ))}
              </TableRow>
            ))
          )}
        </TableBody>
      </Table>
    </div>
  )
}
