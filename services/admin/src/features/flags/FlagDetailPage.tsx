import { Eye, EyeOff, Pencil } from 'lucide-react'
import { useMemo, useState } from 'react'
import { Link, useParams, useSearchParams } from 'react-router-dom'

import { AUDIT_LIMIT_DEFAULT } from '@/api/config'
import type { FlagConfig } from '@/api/types/flags'
import { CopyButton } from '@/components/shared/CopyButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { StatePill } from '@/components/shared/StatePill'
import { VariantSplitBar } from '@/components/shared/VariantSplitBar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { Tabs, TabsContent, TabsList, TabsTrigger } from '@/components/ui/tabs'
import { useFlagAuditQuery, useFlagsQuery } from '@/features/flags/hooks'
import {
  ACTION_LABELS,
  availableActions,
  LifecycleDialog,
  type LifecycleAction,
} from '@/features/flags/LifecycleDialog'
import { AuditTab } from '@/features/flags/tabs/AuditTab'
import { GuardrailsTab } from '@/features/flags/tabs/GuardrailsTab'
import { TargetingTab } from '@/features/flags/tabs/TargetingTab'
import { TesterTab } from '@/features/flags/tabs/TesterTab'
import { JsonView } from '@/components/shared/JsonView'
import { formatDateTime, isPastDate } from '@/lib/format'
import { cn } from '@/lib/utils'

const TABS = ['overview', 'targeting', 'guardrails', 'audit', 'tester'] as const
type TabName = (typeof TABS)[number]

function SaltField({ salt }: { salt: string }) {
  const [revealed, setRevealed] = useState(false)
  return (
    <span className="flex items-center gap-1">
      <code className="font-mono text-xs">{revealed ? salt : '•'.repeat(12)}</code>
      <Button
        variant="ghost"
        size="icon"
        className="h-6 w-6"
        onClick={() => setRevealed((value) => !value)}
        aria-label={revealed ? 'Hide salt' : 'Reveal salt'}
      >
        {revealed ? <EyeOff className="h-3.5 w-3.5" /> : <Eye className="h-3.5 w-3.5" />}
      </Button>
      <CopyButton value={salt} label="Copy salt" />
    </span>
  )
}

function DefinitionRow({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex items-start justify-between gap-4 py-2">
      <dt className="shrink-0 text-sm text-muted-foreground">{label}</dt>
      <dd className="text-right text-sm">{children}</dd>
    </div>
  )
}

function OverviewTab({ flag }: { flag: FlagConfig }) {
  const overdue = isPastDate(flag.review_by) && flag.state !== 'archived'
  return (
    <div className="grid items-start gap-4 lg:grid-cols-2">
      <Card>
        <CardHeader>
          <CardTitle>Definition</CardTitle>
        </CardHeader>
        <CardContent>
          <dl className="divide-y">
            <DefinitionRow label="Description">
              {flag.description || <span className="text-muted-foreground">—</span>}
            </DefinitionRow>
            <DefinitionRow label="Owners">
              {flag.owners.length > 0 ? (
                flag.owners.join(', ')
              ) : (
                <span className="text-muted-foreground">none</span>
              )}
            </DefinitionRow>
            <DefinitionRow label="Review by">
              {flag.review_by ? (
                <span className={cn('tabular-nums', overdue && 'font-medium text-destructive')}>
                  {flag.review_by}
                  {overdue ? ' (overdue)' : ''}
                </span>
              ) : (
                <span className="text-muted-foreground">not set</span>
              )}
            </DefinitionRow>
            <DefinitionRow label="Created">
              <span title={formatDateTime(flag.created_at)}>
                <RelativeTime value={flag.created_at} />
              </span>
            </DefinitionRow>
            <DefinitionRow label="Updated">
              <RelativeTime value={flag.updated_at} />
            </DefinitionRow>
            {flag.archived_at ? (
              <DefinitionRow label="Archived">
                <RelativeTime value={flag.archived_at} />
              </DefinitionRow>
            ) : null}
            <DefinitionRow label="Evaluation mode">
              <Badge variant="outline">{flag.evaluation_mode}</Badge>
            </DefinitionRow>
            <DefinitionRow label="Auto-disable">
              {flag.auto_disable ? 'enabled' : 'disabled — kill switch will refuse system disables'}
            </DefinitionRow>
            <DefinitionRow label="Salt">
              <SaltField salt={flag.salt} />
            </DefinitionRow>
          </dl>
          <p className="mt-2 text-xs text-muted-foreground">
            The salt is immutable — it is the bucketing identity. Changing bucketing requires a new
            flag.
          </p>
        </CardContent>
      </Card>
      <Card>
        <CardHeader>
          <CardTitle>Variant split</CardTitle>
        </CardHeader>
        <CardContent>
          <VariantSplitBar variants={flag.variants} defaultVariant={flag.default_variant} />
          <p className="mt-3 text-xs text-muted-foreground">
            ★ marks <code className="font-mono">default_variant</code> — served when evaluation
            misses every rollout.
          </p>
        </CardContent>
      </Card>
    </div>
  )
}

export function FlagDetailPage() {
  const { key = '' } = useParams()
  const [searchParams, setSearchParams] = useSearchParams()
  const flagsQuery = useFlagsQuery()
  const [auditLimit, setAuditLimit] = useState(AUDIT_LIMIT_DEFAULT)
  const auditQuery = useFlagAuditQuery(key, auditLimit)
  const [lifecycleAction, setLifecycleAction] = useState<LifecycleAction | null>(null)

  const tabParam = searchParams.get('tab')
  const tab: TabName = TABS.includes(tabParam as TabName) ? (tabParam as TabName) : 'overview'

  const flag = useMemo(
    () => flagsQuery.data?.flags.find((entry) => entry.key === key),
    [flagsQuery.data, key],
  )

  // Evidence for the disabled banner: the most recent disable audit entry.
  const disableEvidence = useMemo(() => {
    if (flag?.state !== 'disabled') return null
    const entry = auditQuery.data?.audit.find(
      (candidate) => candidate.action === 'flag_auto_disabled' || candidate.action === 'flag_disabled',
    )
    return entry && Object.keys(entry.evidence).length > 0 ? entry.evidence : null
  }, [flag?.state, auditQuery.data])

  if (flagsQuery.isPending) {
    return (
      <div className="space-y-4">
        <Skeleton className="h-8 w-1/3" />
        <Skeleton className="h-64 w-full" />
      </div>
    )
  }

  if (flagsQuery.error) {
    return <ErrorState error={flagsQuery.error} onRetry={() => void flagsQuery.refetch()} />
  }

  if (!flag) {
    return (
      <EmptyState
        title={`Flag "${key}" not found`}
        description="It may have been archived or deleted in another session — the list refreshes automatically over SSE."
      >
        <Button variant="outline" asChild>
          <a href="/flags">Back to flags</a>
        </Button>
      </EmptyState>
    )
  }

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/flags', label: 'Flags' }}
        title={
          <span className="flex flex-wrap items-center gap-2">
            <code className="font-mono">{flag.key}</code>
            <CopyButton value={flag.key} label="Copy key" />
            <StatePill state={flag.state} />
            <Badge variant="secondary" className="tabular-nums">
              v{flag.version}
            </Badge>
            <Badge variant="outline">{flag.evaluation_mode}</Badge>
          </span>
        }
        description={flag.name}
        actions={
          flag.state !== 'archived' ? (
            <>
              <Button variant="outline" size="sm" asChild>
                <Link to={`/flags/${encodeURIComponent(flag.key)}/edit`}>
                  <Pencil />
                  Edit
                </Link>
              </Button>
              {availableActions(flag).map((action) => (
                <Button
                  key={action}
                  variant={action === 'disable' || action === 'archive' ? 'destructive' : 'outline'}
                  size="sm"
                  onClick={() => setLifecycleAction(action)}
                >
                  {ACTION_LABELS[action]}
                </Button>
              ))}
            </>
          ) : null
        }
      />

      {lifecycleAction ? (
        <LifecycleDialog flag={flag} action={lifecycleAction} onClose={() => setLifecycleAction(null)} />
      ) : null}

      {flag.state === 'disabled' ? (
        <div className="space-y-2 rounded-lg border border-amber-300 bg-amber-50 p-4 text-sm dark:border-amber-900 dark:bg-amber-950/30">
          <p>
            <span className="font-medium">Disabled</span>
            {flag.disabled_reason ? (
              <>
                {' — '}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">{flag.disabled_reason}</code>
              </>
            ) : null}
            {flag.disabled_by ? ` by ${flag.disabled_by}` : ''}
            {flag.disabled_at ? (
              <>
                {' '}
                <RelativeTime value={flag.disabled_at} className="text-muted-foreground" />
              </>
            ) : null}
            . SDK clients fall back to default behavior.
          </p>
          {disableEvidence ? (
            <details>
              <summary className="cursor-pointer text-xs text-muted-foreground">
                Triggering evidence (from audit)
              </summary>
              <JsonView data={disableEvidence} className="mt-2" />
            </details>
          ) : null}
        </div>
      ) : null}

      {flag.state === 'archived' ? (
        <div className="rounded-lg border bg-muted/50 p-4 text-sm text-muted-foreground">
          Archived — terminal state. This flag no longer serves traffic.
        </div>
      ) : null}

      <Tabs
        value={tab}
        onValueChange={(value) =>
          setSearchParams(
            (previous) => {
              const next = new URLSearchParams(previous)
              if (value === 'overview') next.delete('tab')
              else next.set('tab', value)
              return next
            },
            { replace: true },
          )
        }
      >
        <TabsList>
          <TabsTrigger value="overview">Overview</TabsTrigger>
          <TabsTrigger value="targeting">Targeting</TabsTrigger>
          <TabsTrigger value="guardrails">Guardrails</TabsTrigger>
          <TabsTrigger value="audit">Audit</TabsTrigger>
          <TabsTrigger value="tester">Tester</TabsTrigger>
        </TabsList>
        <TabsContent value="overview">
          <OverviewTab flag={flag} />
        </TabsContent>
        <TabsContent value="targeting">
          <TargetingTab flag={flag} />
        </TabsContent>
        <TabsContent value="guardrails">
          <GuardrailsTab flag={flag} />
        </TabsContent>
        <TabsContent value="audit">
          <AuditTab
            flagKey={flag.key}
            query={auditQuery}
            limit={auditLimit}
            onLoadMore={() => setAuditLimit((limit) => Math.min(limit + 50, 200))}
          />
        </TabsContent>
        <TabsContent value="tester">
          <TesterTab flag={flag} />
        </TabsContent>
      </Tabs>
    </div>
  )
}
