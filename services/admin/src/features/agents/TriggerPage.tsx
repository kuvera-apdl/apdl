// Trigger an agent run (plan §5.6.1) — the gating matrix is rendered inline
// straight from the gate's semantics (gatingMatrix.ts, drift-tested).
import { Play } from 'lucide-react'
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { listAgentDefinitions, triggerRun, triggerRunCurl } from '@/api/agents'
import { ApiError } from '@/api/http'
import { CurlButton } from '@/components/shared/CurlButton'
import { Badge } from '@/components/ui/badge'
import { PageHeader } from '@/components/shared/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { queryKeys } from '@/core/queryClient'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'
import { AgentRoleUnavailable } from '@/features/agents/AgentAccessNotice'
import { AUTONOMY_LEVELS, MATRIX_ROWS, type GateOutcome } from '@/features/agents/gatingMatrix'
import { cn } from '@/lib/utils'
import { useQuery } from '@tanstack/react-query'

interface AnalysisOption {
  type: string
  label: string
  description: string
  isCustom: boolean
}

// code_implementation is deliberately absent from the manual trigger list —
// it runs via the approval flow. Filter it out of the live listing too.
const HIDDEN_AGENTS = new Set(['code_implementation', 'experiment_evaluation'])

const OUTCOME_STYLES: Record<GateOutcome, string> = {
  halt: 'text-muted-foreground',
  approve: 'font-medium text-amber-700 dark:text-amber-400',
  deploy: 'font-medium text-emerald-700 dark:text-emerald-400',
}

export function TriggerPage() {
  const { active } = useWorkspace()
  if (!hasWorkspaceRole(active, 'agents:run')) {
    return (
      <div className="max-w-3xl space-y-5">
        <PageHeader
          backTo={{ to: '/agents', label: 'Agent runs' }}
          title="Trigger agent run"
          description="Agent execution is restricted to operator-provisioned workspaces."
        />
        <AgentRoleUnavailable role="agents:run" title="Agent execution unavailable" />
      </div>
    )
  }
  return <TriggerForm />
}

function TriggerForm() {
  const { active, projectId } = useWorkspace()
  const navigate = useNavigate()
  // 'default' runs the full built-in loop; 'custom' hand-picks agents.
  const [mode, setMode] = useState<'default' | 'custom'>('default')
  const [selected, setSelected] = useState<Set<string>>(() => new Set<string>())
  const [timeRangeDays, setTimeRangeDays] = useState(7)
  const [autonomyLevel, setAutonomyLevel] = useState(2)
  const [submitting, setSubmitting] = useState(false)

  const conn = active ? serviceConnection(active, 'agents') : null

  const definitionsQuery = useQuery({
    queryKey:
      active && projectId ? queryKeys.agentDefinitions(active.id, projectId) : ['agent-defs-idle'],
    enabled: Boolean(conn && projectId),
    queryFn: ({ signal }) => listAgentDefinitions(conn!, projectId!, { signal }),
  })

  const analysisTypes: AnalysisOption[] = definitionsQuery.isSuccess
    ? definitionsQuery.data.agents
        .filter((agent) => !HIDDEN_AGENTS.has(agent.name))
        .map((agent) => ({
          type: agent.name,
          label: agent.display_name,
          description: agent.description,
          isCustom: agent.is_custom,
        }))
    : []
  const definitionsReady = definitionsQuery.isSuccess && analysisTypes.length > 0

  // The default loop = every built-in (non-custom) agent. Stable string dep so
  // the sync effect doesn't loop on analysisTypes' fresh array each render.
  const builtinKey = analysisTypes
    .filter((entry) => !entry.isCustom)
    .map((entry) => entry.type)
    .join(',')

  // In default mode the selection tracks the built-in loop (and re-syncs once
  // the server definitions load). Custom mode leaves the user's picks alone.
  useEffect(() => {
    if (mode === 'default') {
      setSelected(new Set(builtinKey ? builtinKey.split(',') : []))
    }
  }, [mode, builtinKey])

  const toggleType = (type: string) => {
    if (mode !== 'custom') return
    setSelected((previous) => {
      const next = new Set(previous)
      if (next.has(type)) next.delete(type)
      else next.add(type)
      return next
    })
  }

  const body =
    definitionsReady && projectId && selected.size > 0
      ? {
          project_id: projectId,
          trigger_type: 'manual' as const,
          analysis_types: analysisTypes
            .map((entry) => entry.type)
            .filter((type) => selected.has(type)),
          time_range_days: timeRangeDays,
          autonomy_level: autonomyLevel,
        }
      : null

  const submit = async () => {
    if (!conn || !active || !body) return
    setSubmitting(true)
    try {
      const response = await triggerRun(conn, body)
      toast.success(`Run ${response.run_id.slice(0, 8)}… started`)
      navigate(`/agents/runs/${encodeURIComponent(response.run_id)}`)
    } catch (error) {
      toast.error(error instanceof ApiError ? error.message : 'Trigger failed')
    } finally {
      setSubmitting(false)
    }
  }

  const dependentsWithoutBase =
    !selected.has('behavior_analysis') &&
    [...selected].some((type) => type !== 'behavior_analysis')

  return (
    <div className="max-w-3xl space-y-5">
      <PageHeader
        backTo={{ to: '/agents', label: 'Agent runs' }}
        title="Trigger agent run"
        description="Launches the supervisor graph in the agents service. Runs invoke the LLM providers configured server-side — reasoning-tier models for analysis, design and proposals."
        actions={
          conn && body ? <CurlButton spec={triggerRunCurl(conn, body)} title="Trigger run" /> : null
        }
      />

      <Card>
        <CardHeader className="flex-row items-start justify-between space-y-0">
          <div className="space-y-1.5">
            <CardTitle>Analysis types</CardTitle>
            <CardDescription>
              {mode === 'default'
                ? 'Runs the full built-in loop. Switch to custom to pick agents.'
                : 'experiment_design and feature_proposal require behavior_analysis insights — runs without it will skip them (unmet requirements).'}
            </CardDescription>
          </div>
          <div className="flex shrink-0 gap-1" role="group" aria-label="Selection mode">
            <Button
              size="sm"
              variant={mode === 'default' ? 'default' : 'outline'}
              onClick={() => setMode('default')}
            >
              Default
            </Button>
            <Button
              size="sm"
              variant={mode === 'custom' ? 'default' : 'outline'}
              onClick={() => setMode('custom')}
            >
              Custom
            </Button>
          </div>
        </CardHeader>
        <CardContent className="space-y-2">
          {definitionsQuery.isPending ? (
            <p className="rounded-md border p-3 text-sm text-muted-foreground" role="status">
              Loading executable agent definitions…
            </p>
          ) : null}
          {definitionsQuery.isError ? (
            <p
              className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
              role="alert"
            >
              Agent definitions are unavailable. Starting a run is disabled until the agents
              service returns a valid capability list.
            </p>
          ) : null}
          {definitionsQuery.isSuccess && analysisTypes.length === 0 ? (
            <p
              className="rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm text-destructive"
              role="alert"
            >
              No executable agent definitions are available for this project. Starting a run is
              disabled.
            </p>
          ) : null}
          {analysisTypes.map((entry) => (
            <label
              key={entry.type}
              className={cn(
                'flex items-start gap-3 rounded-md border p-3 has-[:checked]:border-foreground',
                mode === 'custom' ? 'cursor-pointer' : 'cursor-default opacity-70',
              )}
            >
              <input
                type="checkbox"
                checked={selected.has(entry.type)}
                onChange={() => toggleType(entry.type)}
                disabled={mode !== 'custom'}
                className="mt-1 accent-foreground"
              />
              <span>
                <span className="block text-sm font-medium">
                  {entry.label}
                  {entry.isCustom ? (
                    <Badge variant="secondary" className="ml-2">
                      custom
                    </Badge>
                  ) : null}
                </span>
                <span className="block text-xs text-muted-foreground">{entry.description}</span>
              </span>
            </label>
          ))}
          {dependentsWithoutBase ? (
            <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
              Without behavior_analysis, the selected agents will be skipped — they require its
              insights.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>History window</CardTitle>
          <CardDescription>How much event history behavior analysis reads.</CardDescription>
        </CardHeader>
        <CardContent className="flex items-center gap-3">
          <input
            type="range"
            min={1}
            max={90}
            value={timeRangeDays}
            onChange={(event) => setTimeRangeDays(Number(event.target.value))}
            className="w-56 accent-foreground"
            aria-label="Time range days"
          />
          <Input
            type="number"
            min={1}
            max={90}
            value={timeRangeDays}
            onChange={(event) =>
              setTimeRangeDays(Math.min(90, Math.max(1, Number(event.target.value) || 7)))
            }
            className="w-20 tabular-nums"
            aria-label="Time range days value"
          />
          <span className="text-sm text-muted-foreground">days</span>
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <CardTitle>Autonomy level</CardTitle>
          <CardDescription>
            Feature proposals always require approval; failed safety checks always halt.
          </CardDescription>
        </CardHeader>
        <CardContent className="space-y-4">
          <div className="grid gap-2 sm:grid-cols-2">
            {AUTONOMY_LEVELS.map((def) => (
              <label
                key={def.level}
                className="flex cursor-pointer items-start gap-2 rounded-md border p-3 has-[:checked]:border-foreground"
              >
                <input
                  type="radio"
                  name="autonomy"
                  value={def.level}
                  checked={autonomyLevel === def.level}
                  onChange={() => setAutonomyLevel(def.level)}
                  className="mt-1 accent-foreground"
                />
                <span>
                  <span className="block text-sm font-medium">
                    {def.label}
                    {def.recommended ? (
                      <span className="ml-2 rounded-full bg-secondary px-2 py-0.5 text-xs">
                        recommended
                      </span>
                    ) : null}
                  </span>
                  <span className="block text-xs text-muted-foreground">{def.summary}</span>
                </span>
              </label>
            ))}
          </div>

          <div className="overflow-auto rounded-md border">
            <table className="w-full text-sm">
              <thead>
                <tr className="border-b bg-muted/40 text-left text-xs text-muted-foreground">
                  <th className="p-2 font-medium">Safety result</th>
                  {AUTONOMY_LEVELS.map((def) => (
                    <th
                      key={def.level}
                      className={cn('p-2 text-center font-medium', autonomyLevel === def.level && 'text-foreground')}
                    >
                      {def.label}
                    </th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {MATRIX_ROWS.map((row) => (
                  <tr key={row.label} className="border-b last:border-0">
                    <td className="p-2">{row.label}</td>
                    {AUTONOMY_LEVELS.map((def) => {
                      const outcome = row.outcomes(def.level)
                      return (
                        <td
                          key={def.level}
                          className={cn(
                            'p-2 text-center',
                            OUTCOME_STYLES[outcome],
                            autonomyLevel === def.level && 'bg-accent/40',
                          )}
                        >
                          {outcome}
                        </td>
                      )
                    })}
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <p className="text-xs text-muted-foreground">
            Matrix mirrors <code className="font-mono">framework/gating.py</code> exactly — the
            console tests it against the gate's semantics. Trigger type is fixed to{' '}
            <code className="font-mono">manual</code>; scheduled and threshold_alert are
            external-caller values.
          </p>
        </CardContent>
      </Card>

      <Button onClick={() => void submit()} disabled={submitting || !body}>
        <Play />
        Start run
      </Button>
    </div>
  )
}
