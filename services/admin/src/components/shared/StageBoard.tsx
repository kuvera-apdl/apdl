import type { ReactNode } from 'react'

import { SectionHeading } from '@/components/shared/SectionHeading'
import { cn } from '@/lib/utils'

export interface BoardColumn<T> {
  key: string
  title: string
  items: T[]
}

// A horizontally-scrolling column board — the reusable layout behind Watch's
// stage board (and any future "kanban" of loop objects). Generic over the item
// type; the caller supplies how one item renders.
export function StageBoard<T>({
  columns,
  renderItem,
  emptyLabel = 'nothing here',
  className,
}: {
  columns: BoardColumn<T>[]
  renderItem: (item: T) => ReactNode
  emptyLabel?: string
  className?: string
}) {
  return (
    <div className={cn('grid gap-3 md:grid-cols-2 xl:grid-cols-4', className)}>
      {columns.map((column) => (
        <div key={column.key} className="space-y-2">
          <SectionHeading title={column.title} count={column.items.length} />
          {column.items.length === 0 ? (
            <p className="rounded-lg border border-dashed px-3 py-6 text-center text-xs text-muted-foreground">
              {emptyLabel}
            </p>
          ) : (
            column.items.map((item) => renderItem(item))
          )}
        </div>
      ))}
    </div>
  )
}
