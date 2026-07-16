// Per-run agent audit trail over HTTP (gap G2): action_type, approval_status,
// safety checks with pass/fail and risk level, full config payloads.
import { useQuery } from '@tanstack/react-query'
import { CheckCircle2, XCircle } from 'lucide-react'

import { runAudit } from '@/api/agents'
import type { RunAuditEntry } from '@/api/types/agents'
import { JsonView } from '@/components/shared/JsonView'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { cn } from '@/lib/utils'

const APPROVAL_STYLES: Record<string, string> = {
  approved:
    'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  rejected: 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300',
  pending:
    'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
}

interface SafetyCheck {
  name?: unknown
  passed?: unknown
  detail?: unknown
}

function SafetyResult({ safety }: { safety: Record<string, unknown> }) {
  if (Object.keys(safety).length === 0) return null
  const checks = Array.isArray(safety.checks) ? (safety.checks as SafetyCheck[]) : null
  const risk = typeof safety.risk_level === 'string' ? safety.risk_level : null
  return (
    <div className="space-y-1">
      {risk ? (
        <p className="text-xs">
          Risk level: <Badge variant="secondary">{risk}</Badge>
        </p>
      ) : null}
      {checks ? (
        <ul className="space-y-0.5 text-xs">
          {checks.map((check, index) => (
            <li key={index} className="flex items-center gap-1.5">
              {check.passed ? (
                <CheckCircle2 className="h-3 w-3 shrink-0 text-emerald-600" />
              ) : (
                <XCircle className="h-3 w-3 shrink-0 text-destructive" />
              )}
              <span>{typeof check.name === 'string' ? check.name : `check ${index + 1}`}</span>
              {typeof check.detail === 'string' && check.detail ? (
                <span className="text-muted-foreground">— {check.detail}</span>
              ) : null}
            </li>
          ))}
        </ul>
      ) : (
        <JsonView data={safety} className="max-h-32" />
      )}
    </div>
  )
}

export function AuditEntryRow({ entry }: { entry: RunAuditEntry }) {
  const hasConfig = Object.keys(entry.config).length > 0
  const hasSafety = Object.keys(entry.safety_result).length > 0
  return (
    <li className="space-y-1.5 border-l-2 border-l-border py-2.5 pl-4">
      <div className="flex flex-wrap items-center gap-2 text-sm">
        <code className="rounded bg-muted px-1.5 py-0.5 font-mono text-xs">{entry.action_type}</code>
        {entry.approval_status ? (
          <span
            className={cn(
              'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
              APPROVAL_STYLES[entry.approval_status] ?? 'bg-secondary',
            )}
          >
            {entry.approval_status}
          </span>
        ) : null}
        <RelativeTime value={entry.created_at} className="ml-auto text-xs text-muted-foreground" />
      </div>
      {hasSafety ? <SafetyResult safety={entry.safety_result} /> : null}
      {hasConfig ? (
        <details>
          <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
            Config
          </summary>
          <JsonView data={entry.config} className="mt-1 max-h-48" />
        </details>
      ) : null}
    </li>
  )
}

export function RunAuditSection({ runId }: { runId: string }) {
  const { active } = useWorkspace()
  const auditQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run', runId, 'audit'],
    enabled: active !== null && runId !== '',
    staleTime: 10_000,
    queryFn: ({ signal }) => runAudit(serviceConnection(active!, 'agents'), runId, { signal }),
  })

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agent audit trail</CardTitle>
        <CardDescription>
          Every action, decision, and safety verdict this run recorded (newest first).
        </CardDescription>
      </CardHeader>
      <CardContent>
        {auditQuery.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : auditQuery.error ? (
          <p className="text-sm text-muted-foreground">
            Audit unavailable — the agents service may predate the audit endpoint.
          </p>
        ) : auditQuery.data && auditQuery.data.audit.length > 0 ? (
          <ul>
            {auditQuery.data.audit.map((entry) => (
              <AuditEntryRow key={entry.id} entry={entry} />
            ))}
          </ul>
        ) : (
          <p className="text-sm text-muted-foreground">No audit entries yet.</p>
        )}
      </CardContent>
    </Card>
  )
}
