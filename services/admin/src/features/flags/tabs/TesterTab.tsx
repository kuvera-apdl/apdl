// Evaluation tester (plan §5.3.6): answers "what does user X get and why"
// without server round-trips (AD-4) — the local FNV-1a evaluator is
// parity-tested against fixtures/gates/parity.json. Server verification is an
// optional extra when an internal token is configured.
import { CheckCircle2, Loader2, Plus, Trash2, XCircle } from 'lucide-react'
import { useMemo, useState } from 'react'

import { evaluateFlagOnServer } from '@/api/config'
import type { FlagConfig, GateEvaluateResponse } from '@/api/types/flags'
import { JsonView } from '@/components/shared/JsonView'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import {
  evaluateFlagDetailed,
  type EvaluationResult,
  type FlagEvaluation,
  type RuleTrace,
} from '@/core/evaluator/evaluate'
import { useLive } from '@/core/live'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { PopulationSimulator } from '@/features/flags/tester/PopulationSimulator'
import { formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

type AttributeType = 'string' | 'number' | 'boolean'

interface AttributeRow {
  key: string
  value: string
  type: AttributeType
}

function buildAttributes(rows: AttributeRow[]): Record<string, unknown> {
  const attributes: Record<string, unknown> = {}
  for (const row of rows) {
    if (row.key.trim() === '') continue
    if (row.type === 'number') attributes[row.key.trim()] = Number(row.value)
    else if (row.type === 'boolean') attributes[row.key.trim()] = row.value === 'true'
    else attributes[row.key.trim()] = row.value
  }
  return attributes
}

const REASON_STYLES: Record<string, string> = {
  rule_match: 'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  fallthrough: 'border-emerald-200 bg-emerald-100 text-emerald-800 dark:border-emerald-900 dark:bg-emerald-950/60 dark:text-emerald-300',
  rule_rollout: 'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
  fallthrough_rollout: 'border-amber-200 bg-amber-100 text-amber-800 dark:border-amber-900 dark:bg-amber-950/60 dark:text-amber-300',
  disabled: 'border-slate-200 bg-slate-100 text-slate-700 dark:border-slate-700 dark:bg-slate-800/60 dark:text-slate-300',
  error: 'border-red-200 bg-red-100 text-red-800 dark:border-red-900 dark:bg-red-950/60 dark:text-red-300',
}

function explain(evaluation: FlagEvaluation, flag: FlagConfig): string {
  const { result } = evaluation
  const ruleName = (id: string | null) => {
    const trace = evaluation.rules.find((entry) => entry.rule.id === id)
    return trace ? trace.rule.name || trace.rule.id : id ?? ''
  }
  const pct = result.rollout_percentage !== null ? formatPercent(result.rollout_percentage) : '?'
  const bucket = result.rollout_bucket !== null ? result.rollout_bucket.toFixed(1) : '?'
  switch (result.reason) {
    case 'rule_match':
      return `Matched rule "${ruleName(result.rule_id)}" — rollout bucket ${bucket} < ${pct}; variant bucket ${result.variant_bucket?.toFixed(1) ?? '?'} → ${result.variant}.`
    case 'rule_rollout':
      return `Conditions of rule "${ruleName(result.rule_id)}" matched, but the rollout missed (bucket ${bucket} ≥ ${pct}) — default variant "${result.variant}" served.`
    case 'fallthrough':
      return `No rule matched; fallthrough rollout passed (bucket ${bucket} < ${pct}); variant bucket ${result.variant_bucket?.toFixed(1) ?? '?'} → ${result.variant}.`
    case 'fallthrough_rollout':
      return `No rule matched and the fallthrough rollout missed (bucket ${bucket} ≥ ${pct}) — default variant "${result.variant}" served.`
    case 'disabled':
      return `The flag is disabled — default variant "${flag.default_variant}" is served to everyone.`
    case 'error':
      return `No unit id available for bucket_by "${result.bucket_by ?? '?'}" (empty or missing in the context) — evaluation fell back to the default variant.`
    default:
      return ''
  }
}

function BucketBar({ label, bucket, threshold }: { label: string; bucket: number | null; threshold: number | null }) {
  if (bucket === null) return null
  return (
    <div className="space-y-1">
      <div className="flex justify-between text-xs text-muted-foreground">
        <span>{label}</span>
        <span className="tabular-nums">
          {bucket.toFixed(2)}
          {threshold !== null ? ` / threshold ${formatPercent(threshold)}` : ''}
        </span>
      </div>
      <div className="relative h-2.5 w-full rounded-full bg-muted">
        {threshold !== null ? (
          <div className="absolute inset-y-0 left-0 rounded-l-full bg-emerald-500/25" style={{ width: `${threshold}%` }} />
        ) : null}
        <div
          className="absolute top-1/2 h-3.5 w-1 -translate-y-1/2 rounded bg-foreground"
          style={{ left: `calc(${bucket}% - 2px)` }}
          title={bucket.toFixed(3)}
        />
      </div>
    </div>
  )
}

const OUTCOME_LABELS: Record<RuleTrace['outcome'], string> = {
  matched: 'matched',
  conditions_failed: 'conditions failed',
  rollout_missed: 'matched, missed rollout',
  not_reached: 'not reached',
  error: 'error — no unit id',
}

function RuleTraceView({ evaluation }: { evaluation: FlagEvaluation }) {
  return (
    <div className="space-y-2">
      {evaluation.rules.map((trace, index) => {
        const dimmed = trace.outcome === 'not_reached' || trace.outcome === 'conditions_failed'
        return (
          <div
            key={trace.rule.id}
            className={cn(
              'rounded-md border p-3',
              trace.outcome === 'matched' && 'border-emerald-400 dark:border-emerald-700',
              dimmed && 'opacity-60',
            )}
          >
            <div className="flex items-center gap-2 text-sm">
              <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
                {index + 1}
              </span>
              <span className="font-medium">{trace.rule.name || trace.rule.id}</span>
              <Badge variant={trace.outcome === 'matched' ? 'default' : 'secondary'} className="ml-auto">
                {OUTCOME_LABELS[trace.outcome]}
              </Badge>
            </div>
            {trace.conditions.length > 0 ? (
              <ul className="mt-2 space-y-1 text-xs">
                {trace.conditions.map((condition, conditionIndex) => (
                  <li key={conditionIndex} className="flex items-center gap-1.5">
                    {condition.matched ? (
                      <CheckCircle2 className="h-3.5 w-3.5 shrink-0 text-emerald-600" />
                    ) : (
                      <XCircle className="h-3.5 w-3.5 shrink-0 text-destructive" />
                    )}
                    <span className={cn(!condition.matched && 'line-through opacity-70')}>
                      <code className="font-mono">{condition.condition.attribute}</code>{' '}
                      {condition.condition.operator.replace(/_/g, ' ')}{' '}
                      {condition.condition.value !== undefined && condition.condition.value !== null ? (
                        <code className="font-mono">{JSON.stringify(condition.condition.value)}</code>
                      ) : null}
                      <span className="text-muted-foreground">
                        {' '}
                        (actual: {condition.actual.exists ? JSON.stringify(condition.actual.value) : 'missing'})
                      </span>
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
            {trace.rollout && trace.rollout.bucket !== null ? (
              <div className="mt-2">
                <BucketBar label="Rule rollout bucket" bucket={trace.rollout.bucket} threshold={trace.rollout.percentage} />
              </div>
            ) : null}
          </div>
        )
      })}
      <div className={cn('rounded-md border border-dashed p-3 text-sm', !evaluation.fallthrough.reached && 'opacity-60')}>
        <span className="font-medium">Fallthrough</span>{' '}
        <span className="text-xs text-muted-foreground">
          {evaluation.fallthrough.reached ? 'reached' : 'not reached'}
        </span>
        {evaluation.fallthrough.rollout && evaluation.fallthrough.rollout.bucket !== null ? (
          <div className="mt-2">
            <BucketBar
              label="Fallthrough rollout bucket"
              bucket={evaluation.fallthrough.rollout.bucket}
              threshold={evaluation.fallthrough.rollout.percentage}
            />
          </div>
        ) : null}
      </div>
    </div>
  )
}

const VERIFY_FIELDS: (keyof EvaluationResult)[] = [
  'variant',
  'reason',
  'rule_id',
  'rollout_bucket',
  'variant_bucket',
  'rollout_percentage',
  'bucket_by',
  'config_version',
]

export function TesterTab({ flag }: { flag: FlagConfig }) {
  const { active, projectId } = useWorkspace()
  const { servedFlags } = useLive()

  const [userId, setUserId] = useState('user_123')
  const [anonymousId, setAnonymousId] = useState('')
  const [rows, setRows] = useState<AttributeRow[]>([])
  const [serverResult, setServerResult] = useState<GateEvaluateResponse | null>(null)
  const [serverPending, setServerPending] = useState(false)
  const [serverError, setServerError] = useState<string | null>(null)

  const attributes = useMemo(() => buildAttributes(rows), [rows])
  const evaluation = useMemo(
    () => evaluateFlagDetailed(flag, { user_id: userId, anonymous_id: anonymousId, attributes }),
    [flag, userId, anonymousId, attributes],
  )
  const { result } = evaluation

  const canVerify = active !== null && flag.evaluation_mode !== 'client'

  const verifyOnServer = async () => {
    if (!active || !projectId) return
    setServerPending(true)
    setServerError(null)
    setServerResult(null)
    try {
      const response = await evaluateFlagOnServer(serviceConnection(active, 'config'), {
        project_id: projectId,
        key: flag.key,
        context: { user_id: userId, anonymous_id: anonymousId, attributes },
        log_exposure: false,
      })
      setServerResult(response)
    } catch (caught) {
      setServerError(caught instanceof Error ? caught.message : 'Verification failed')
    } finally {
      setServerPending(false)
    }
  }

  const serverMatches =
    serverResult !== null && VERIFY_FIELDS.every((field) => Object.is(serverResult[field], result[field]))

  const served = servedFlags?.get(flag.key) ?? null

  const updateRow = (index: number, patch: Partial<AttributeRow>) => {
    setRows((previous) => previous.map((row, rowIndex) => (rowIndex === index ? { ...row, ...patch } : row)))
  }

  return (
    <div className="space-y-4">
      <div className="grid items-start gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Context</CardTitle>
            <CardDescription>The user being evaluated — edits re-evaluate instantly.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="grid gap-3 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="tester-user-id">user_id</Label>
                <Input
                  id="tester-user-id"
                  value={userId}
                  onChange={(event) => setUserId(event.target.value)}
                  className="font-mono text-xs"
                />
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="tester-anonymous-id">anonymous_id</Label>
                <Input
                  id="tester-anonymous-id"
                  value={anonymousId}
                  onChange={(event) => setAnonymousId(event.target.value)}
                  className="font-mono text-xs"
                />
              </div>
            </div>
            <div className="space-y-2">
              <Label>Attributes</Label>
              {rows.map((row, index) => (
                <div key={index} className="flex items-center gap-2">
                  <Input
                    placeholder="key"
                    value={row.key}
                    onChange={(event) => updateRow(index, { key: event.target.value })}
                    className="w-36 font-mono text-xs"
                    aria-label={`Attribute ${index + 1} key`}
                  />
                  <Select
                    value={row.type}
                    onChange={(event) => updateRow(index, { type: event.target.value as AttributeType })}
                    className="w-28"
                    aria-label={`Attribute ${index + 1} type`}
                  >
                    <option value="string">string</option>
                    <option value="number">number</option>
                    <option value="boolean">boolean</option>
                  </Select>
                  {row.type === 'boolean' ? (
                    <Select
                      value={row.value}
                      onChange={(event) => updateRow(index, { value: event.target.value })}
                      className="flex-1"
                      aria-label={`Attribute ${index + 1} value`}
                    >
                      <option value="true">true</option>
                      <option value="false">false</option>
                    </Select>
                  ) : (
                    <Input
                      placeholder="value"
                      type={row.type === 'number' ? 'number' : 'text'}
                      step="any"
                      value={row.value}
                      onChange={(event) => updateRow(index, { value: event.target.value })}
                      className="flex-1"
                      aria-label={`Attribute ${index + 1} value`}
                    />
                  )}
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon"
                    onClick={() => setRows((previous) => previous.filter((_, rowIndex) => rowIndex !== index))}
                    aria-label={`Remove attribute ${index + 1}`}
                  >
                    <Trash2 />
                  </Button>
                </div>
              ))}
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={() => setRows((previous) => [...previous, { key: '', value: '', type: 'string' }])}
              >
                <Plus />
                Add attribute
              </Button>
            </div>
          </CardContent>
        </Card>

        <Card>
          <CardHeader className="flex-row items-start justify-between space-y-0">
            <div className="space-y-1.5">
              <CardTitle>Result</CardTitle>
              <CardDescription>Local evaluation — identical to SDK bucketing.</CardDescription>
            </div>
            {canVerify ? (
              <Button type="button" variant="outline" size="sm" onClick={() => void verifyOnServer()} disabled={serverPending}>
                {serverPending ? <Loader2 className="animate-spin" /> : null}
                Verify on server
              </Button>
            ) : null}
          </CardHeader>
          <CardContent className="space-y-4">
            <div className="flex items-center gap-3">
              <span className="font-mono text-2xl font-semibold">{result.variant ?? '—'}</span>
              <span
                className={cn(
                  'inline-flex items-center rounded-full border px-2 py-0.5 text-xs font-medium',
                  REASON_STYLES[result.reason] ?? 'bg-muted',
                )}
              >
                {result.reason}
              </span>
              <span className="ml-auto text-xs text-muted-foreground">config v{result.config_version}</span>
            </div>
            <p className="text-sm text-muted-foreground">{explain(evaluation, flag)}</p>
            <div className="space-y-3">
              <BucketBar label="Rollout bucket" bucket={result.rollout_bucket} threshold={result.rollout_percentage} />
              <BucketBar label="Variant bucket" bucket={result.variant_bucket} threshold={null} />
            </div>
            {!canVerify ? (
              <p className="text-xs text-muted-foreground">
                {flag.evaluation_mode === 'client'
                  ? 'Server verification is unavailable for client-mode flags (the server refuses them).'
                  : 'Configure an internal token in the workspace to enable server verification.'}
              </p>
            ) : null}
            {serverError ? (
              <p className="rounded-md border border-destructive/50 bg-destructive/10 p-2 text-sm text-destructive">
                {serverError}
              </p>
            ) : null}
            {serverResult ? (
              serverMatches ? (
                <p className="flex items-center gap-2 rounded-md border border-emerald-300 bg-emerald-50 p-2 text-sm dark:border-emerald-900 dark:bg-emerald-950/40">
                  <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                  Server result matches the local evaluation exactly.
                </p>
              ) : (
                <div className="space-y-2 rounded-md border border-destructive/60 bg-destructive/10 p-2 text-sm">
                  <p className="flex items-center gap-2 font-medium text-destructive">
                    <XCircle className="h-4 w-4" />
                    Parity mismatch — local and server evaluation disagree. This is worth a bug report.
                  </p>
                  <JsonView data={{ local: result, server: serverResult }} className="max-h-48" />
                </div>
              )
            ) : null}
            <p className="border-t pt-2 text-xs text-muted-foreground">
              Evaluator parity-tested against <code className="font-mono">fixtures/gates/parity.json</code>.
            </p>
          </CardContent>
        </Card>
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Rule trace</CardTitle>
          <CardDescription>
            Rules in evaluation order — the matched rule is highlighted, failing conditions struck
            through.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {evaluation.rules.length === 0 ? (
            <p className="text-sm text-muted-foreground">No rules — evaluation goes straight to fallthrough.</p>
          ) : null}
          <RuleTraceView evaluation={evaluation} />
        </CardContent>
      </Card>

      <div className="grid items-start gap-4 lg:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle>Population simulation</CardTitle>
            <CardDescription>
              10,000 synthetic unit ids through this config. All simulated users share the
              attributes entered above; only the unit id varies.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <PopulationSimulator
              flag={{ ...flag, enabled: true }}
              attributes={attributes}
            />
            {!flag.enabled ? (
              <p className="mt-2 text-xs text-muted-foreground">
                The flag is currently {flag.state} — the simulation shows what the config would do
                once active.
              </p>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Served config</CardTitle>
            <CardDescription>
              What SDKs currently receive over SSE for this flag — exactly the client payload.
            </CardDescription>
          </CardHeader>
          <CardContent>
            {served ? (
              <JsonView data={served} className="max-h-80" />
            ) : (
              <p className="text-sm text-muted-foreground">
                Not in the live SDK payload — the flag is server-mode, archived, not client-visible,
                or the SSE stream has not delivered a config snapshot yet.
              </p>
            )}
          </CardContent>
        </Card>
      </div>
    </div>
  )
}
