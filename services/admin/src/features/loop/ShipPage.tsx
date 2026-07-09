// Ship — what actually changed in the product (admin-console-purpose-ia.md).
// The ledger of live treatments and rollbacks, read from the flags registry:
// experiment-backed flags are the loop's shipping mechanism. Merged durable
// features join here via the codegen changeset registry (linked out).
import { useQuery } from '@tanstack/react-query'
import { GitPullRequest, Package } from 'lucide-react'
import { Link } from 'react-router-dom'

import { listFlags } from '@/api/config'
import type { FlagConfig } from '@/api/types/flags'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { SectionHeading } from '@/components/shared/SectionHeading'
import { StatePill } from '@/components/shared/StatePill'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'

// Loop-shipped flags: experiment-backed (key convention exp_, or multi-variant).
function isExperimentFlag(flag: FlagConfig): boolean {
  return flag.key.startsWith('exp_') || (flag.variants?.length ?? 0) > 1
}

function FlagRow({ flag }: { flag: FlagConfig }) {
  return (
    <Link to={`/flags/${encodeURIComponent(flag.key)}`}>
      <Card className="transition-colors hover:border-foreground/20">
        <CardContent className="flex items-center justify-between gap-3 p-3">
          <div className="min-w-0">
            <p className="truncate text-sm font-medium">{flag.name || flag.key}</p>
            <code className="font-mono text-xs text-muted-foreground">{flag.key}</code>
          </div>
          <div className="flex shrink-0 items-center gap-3">
            <StatePill state={flag.state} />
            <RelativeTime value={flag.updated_at} className="text-xs text-muted-foreground" />
          </div>
        </CardContent>
      </Card>
    </Link>
  )
}

export function ShipPage() {
  const { active } = useWorkspace()
  const conn = active ? serviceConnection(active, 'config') : null

  const flagsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'ship', 'flags'],
    enabled: Boolean(conn),
    queryFn: ({ signal }) => listFlags(conn!, { signal }),
  })

  const flags = (flagsQuery.data?.flags ?? []).filter(isExperimentFlag)
  const live = flags.filter((f) => f.state === 'active')
  const rolledBack = flags.filter((f) => f.state === 'disabled')

  return (
    <div className="space-y-5">
      <PageHeader
        title="Ship"
        description="What the loop has changed in your product — live treatments and rollbacks."
        actions={
          <Button size="sm" variant="outline" asChild>
            <Link to="/codegen">
              <GitPullRequest />
              Pull requests
            </Link>
          </Button>
        }
      />

      {flagsQuery.isPending ? <Skeleton className="h-40 w-full" /> : null}
      {flagsQuery.error ? (
        <ErrorState error={flagsQuery.error} onRetry={() => void flagsQuery.refetch()} />
      ) : null}

      {flagsQuery.data && flags.length === 0 ? (
        <EmptyState
          icon={<Package className="h-8 w-8" />}
          title="Nothing shipped yet"
          description="Treatments the loop builds and features it makes permanent will appear here."
        />
      ) : null}

      {live.length > 0 ? (
        <div className="space-y-2">
          <SectionHeading title="Live behind flags" count={live.length} />
          {live.map((flag) => (
            <FlagRow key={flag.key} flag={flag} />
          ))}
        </div>
      ) : null}

      {rolledBack.length > 0 ? (
        <div className="space-y-2">
          <SectionHeading title="Rolled back" count={rolledBack.length} />
          {rolledBack.map((flag) => (
            <FlagRow key={flag.key} flag={flag} />
          ))}
        </div>
      ) : null}
    </div>
  )
}
