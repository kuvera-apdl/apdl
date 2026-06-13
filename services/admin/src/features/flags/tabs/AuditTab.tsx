// Audit timeline (plan §5.3.7): before/after diffs, system actors styled
// distinctly, auto-disables rendered as incidents.
import type { UseQueryResult } from '@tanstack/react-query'

import { AUDIT_LIMIT_MAX, flagAuditCurl } from '@/api/config'
import type { FlagAuditAction, FlagAuditEntry, FlagAuditResponse } from '@/api/types/flags'
import { CurlButton } from '@/components/shared/CurlButton'
import { JsonDiff } from '@/components/shared/JsonDiff'
import { JsonView } from '@/components/shared/JsonView'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { cn } from '@/lib/utils'

const ACTION_STYLES: Record<FlagAuditAction, string> = {
  flag_created:
    'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  flag_updated:
    'border-sky-200 bg-sky-100 text-sky-800 dark:border-sky-900 dark:bg-sky-950/60 dark:text-sky-300',
  flag_disabled:
    'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
  flag_auto_disabled:
    'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300',
  flag_archived: 'border-transparent bg-muted text-muted-foreground',
  flag_cleanup_archived:
    'border-violet-200 bg-violet-100 text-violet-800 dark:border-violet-900 dark:bg-violet-950/60 dark:text-violet-300',
}

// The guardrail monitor and agent rollbacks write system-attributed entries.
const SYSTEM_ACTORS = new Set(['system', 'guardrail-monitor'])

function AuditEntryItem({ entry }: { entry: FlagAuditEntry }) {
  const incident = entry.action === 'flag_auto_disabled'
  const hasEvidence = Object.keys(entry.evidence).length > 0
  return (
    <li
      className={cn(
        'space-y-2 border-l-2 py-3 pl-4',
        incident ? 'border-l-red-500' : 'border-l-border',
      )}
    >
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <span
          className={cn(
            'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
            ACTION_STYLES[entry.action],
          )}
        >
          {entry.action.replace('flag_', '').replace(/_/g, ' ')}
        </span>
        <Badge variant={SYSTEM_ACTORS.has(entry.actor) ? 'destructive' : 'secondary'}>
          {entry.actor}
        </Badge>
        {entry.previous_version !== null || entry.new_version !== null ? (
          <span className="tabular-nums text-muted-foreground">
            v{entry.previous_version ?? '∅'} → v{entry.new_version ?? '∅'}
          </span>
        ) : null}
        {entry.reason ? <span className="text-muted-foreground">· {entry.reason}</span> : null}
        <RelativeTime value={entry.created_at} className="ml-auto text-xs text-muted-foreground" />
      </div>
      {entry.before !== null || entry.after !== null || hasEvidence ? (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            Changes{hasEvidence ? ' & evidence' : ''}
          </summary>
          <div className="mt-2 space-y-3">
            <JsonDiff before={entry.before} after={entry.after} />
            {hasEvidence ? (
              <div>
                <p className="mb-1 text-xs font-medium text-muted-foreground">Evidence</p>
                <JsonView data={entry.evidence} />
              </div>
            ) : null}
          </div>
        </details>
      ) : null}
    </li>
  )
}

interface AuditTabProps {
  flagKey: string
  query: UseQueryResult<FlagAuditResponse, Error>
  limit: number
  onLoadMore: () => void
}

export function AuditTab({ flagKey, query, limit, onLoadMore }: AuditTabProps) {
  const { active } = useWorkspace()
  const conn = active ? serviceConnection(active, 'config') : null

  if (query.isPending) {
    return (
      <div className="max-w-3xl space-y-3">
        {Array.from({ length: 3 }).map((_, index) => (
          <Skeleton key={index} className="h-16 w-full" />
        ))}
      </div>
    )
  }
  if (query.error) {
    return <ErrorState error={query.error} onRetry={() => void query.refetch()} />
  }

  const entries = query.data?.audit ?? []
  const mayHaveMore = entries.length === limit && limit < AUDIT_LIMIT_MAX

  return (
    <div className="max-w-3xl space-y-4">
      <div className="flex items-center justify-between">
        <p className="text-sm text-muted-foreground">
          {entries.length} entries (newest first, retention-capped at {AUDIT_LIMIT_MAX})
        </p>
        {conn ? <CurlButton spec={flagAuditCurl(conn, flagKey, limit)} title="Flag audit" /> : null}
      </div>
      {entries.length === 0 ? (
        <div className="rounded-lg border">
          <EmptyState title="No audit entries" description="Mutations to this flag will appear here." />
        </div>
      ) : (
        <ul>
          {entries.map((entry) => (
            <AuditEntryItem key={entry.id} entry={entry} />
          ))}
        </ul>
      )}
      {mayHaveMore ? (
        <Button variant="outline" size="sm" onClick={onLoadMore}>
          Load more
        </Button>
      ) : null}
    </div>
  )
}
