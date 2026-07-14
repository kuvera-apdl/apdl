// Per-agent inspector for a run (agents/runs/:id). Each agent that took part in
// the pipeline gets a selector (a Disclosure) that opens its own info: the full
// input it consumed, its text/typed output, and its slice of the audit trail.
import { useQuery } from '@tanstack/react-query'
import { CircleSlash, XCircle } from 'lucide-react'

import { listAgentDefinitions, runAudit } from '@/api/agents'
import type { AgentDefinition, RunAuditEntry, RunResults, RunStatus } from '@/api/types/agents'
import { JsonView } from '@/components/shared/JsonView'
import { SectionHeading } from '@/components/shared/SectionHeading'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Disclosure } from '@/components/ui/disclosure'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { AuditEntryRow } from '@/features/agents/RunAuditSection'
import { ResultCard } from '@/features/agents/ResultCards'

// produces key → ResultCard renderer kind (custom agents fall through).
const PRODUCES_KIND: Record<string, Parameters<typeof ResultCard>[0]['kind']> = {
  insights: 'insight',
  experiment_designs: 'experiment_design',
  personalizations: 'personalization',
  feature_proposals: 'feature_proposal',
  changesets: 'changeset',
}

// Items an agent produced (or consumed, when `key` is one of its requires).
function itemsFor(results: RunResults | null, key: string): unknown[] {
  if (!results) return []
  const direct = (results as unknown as Record<string, unknown>)[key]
  if (Array.isArray(direct)) return direct
  const custom = results.custom_outputs?.[key]
  return Array.isArray(custom) ? custom : []
}

// Audit entries belonging to this agent (action_type is `<name>` or `<name>_…`).
function auditForAgent(audit: RunAuditEntry[], name: string): RunAuditEntry[] {
  return audit.filter((e) => e.action_type === name || e.action_type.startsWith(`${name}_`))
}

interface AgentStatus {
  label: string
  tone: 'done' | 'error' | 'skipped'
}

function agentStatus(entries: RunAuditEntry[]): AgentStatus | null {
  if (entries.some((e) => e.action_type.endsWith('_error'))) return { label: 'errored', tone: 'error' }
  if (entries.some((e) => e.action_type.endsWith('_skipped'))) return { label: 'skipped', tone: 'skipped' }
  if (entries.some((e) => e.action_type.endsWith('_complete'))) return { label: 'completed', tone: 'done' }
  return null
}

function OutputBlock({ agent, results }: { agent: AgentDefinition; results: RunResults | null }) {
  const items = itemsFor(results, agent.produces)
  const kind = PRODUCES_KIND[agent.produces] ?? 'custom'
  if (items.length === 0) {
    return <p className="text-sm text-muted-foreground">No {agent.produces.replace(/_/g, ' ')} produced.</p>
  }
  return (
    <div className="space-y-2">
      {items.map((item, index) => (
        <ResultCard key={index} item={item} kind={kind} />
      ))}
      <Disclosure summary="Raw output (JSON)" className="mt-1">
        <JsonView data={items} className="max-h-72" />
      </Disclosure>
    </div>
  )
}

function InputBlock({ agent, results }: { agent: AgentDefinition; results: RunResults | null }) {
  if (agent.requires.length === 0) {
    return (
      <p className="text-sm text-muted-foreground">
        No upstream input — reads the project&apos;s event warehouse directly via its query tools.
      </p>
    )
  }
  return (
    <div className="space-y-2">
      <div className="flex flex-wrap items-center gap-1.5 text-xs text-muted-foreground">
        <span>Consumes</span>
        {agent.requires.map((req) => (
          <Badge key={req} variant="secondary">
            {req.replace(/_/g, ' ')}
          </Badge>
        ))}
      </div>
      {agent.requires.map((req) => {
        const items = itemsFor(results, req)
        return (
          <Disclosure key={req} summary={req.replace(/_/g, ' ')} count={items.length}>
            {items.length > 0 ? (
              <JsonView data={items} className="max-h-72" />
            ) : (
              <p className="text-sm text-muted-foreground">
                Not captured in this run&apos;s persisted results.
              </p>
            )}
          </Disclosure>
        )
      })}
    </div>
  )
}

function AgentInspector({
  agent,
  results,
  audit,
}: {
  agent: AgentDefinition
  results: RunResults | null
  audit: RunAuditEntry[]
}) {
  const entries = auditForAgent(audit, agent.name)
  const status = agentStatus(entries)
  const outputCount = itemsFor(results, agent.produces).length

  return (
    <Disclosure
      className="bg-card"
      summary={
        <span className="flex flex-wrap items-center gap-2">
          <span className="font-medium">{agent.display_name}</span>
          <Badge variant="outline">{agent.produces.replace(/_/g, ' ')}</Badge>
          {agent.is_custom ? <Badge variant="secondary">custom</Badge> : null}
          {status ? (
            <span className="inline-flex items-center gap-1 text-xs text-muted-foreground">
              {status.tone === 'error' ? <XCircle className="h-3 w-3 text-destructive" /> : null}
              {status.tone === 'skipped' ? <CircleSlash className="h-3 w-3" /> : null}
              {status.label}
            </span>
          ) : null}
        </span>
      }
      trailing={
        <span className="text-xs tabular-nums text-muted-foreground">{outputCount} out</span>
      }
    >
      <div className="space-y-2">
        <p className="text-xs text-muted-foreground">{agent.description}</p>
        <Disclosure summary="Input" className="bg-background">
          <InputBlock agent={agent} results={results} />
        </Disclosure>
        <Disclosure summary="Output" count={outputCount} className="bg-background">
          <OutputBlock agent={agent} results={results} />
        </Disclosure>
        <Disclosure summary="Audit trail" count={entries.length} className="bg-background">
          {entries.length > 0 ? (
            <ul>
              {entries.map((entry) => (
                <AuditEntryRow key={entry.id} entry={entry} />
              ))}
            </ul>
          ) : (
            <p className="text-sm text-muted-foreground">No audit entries for this agent.</p>
          )}
        </Disclosure>
      </div>
    </Disclosure>
  )
}

// The section rendered on the run page: one inspector per agent that
// participated (produced output or left an audit trail), in pipeline order.
export function AgentsInspectorSection({
  runId,
  run,
  results,
}: {
  runId: string
  run: RunStatus
  results: RunResults | null
}) {
  const { active, projectId } = useWorkspace()
  const conn = active ? serviceConnection(active, 'agents') : null

  const definitionsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-defs', projectId ?? 'none'],
    enabled: Boolean(conn && projectId),
    queryFn: ({ signal }) => listAgentDefinitions(conn!, projectId!, { signal }),
  })

  // Same queryKey as RunAuditSection so react-query dedupes the fetch.
  const auditQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'agent-run', runId, 'audit'],
    enabled: Boolean(conn) && runId !== '',
    staleTime: 10_000,
    queryFn: ({ signal }) => runAudit(conn!, runId, { signal }),
  })

  const definitions = definitionsQuery.data?.agents ?? []
  const audit = auditQuery.data?.audit ?? []

  // Participants: agents with output or an audit trail in this run.
  const participants = definitions
    .filter((agent) => itemsFor(results, agent.produces).length > 0 || auditForAgent(audit, agent.name).length > 0)
    .sort((a, b) => a.order - b.order)

  return (
    <Card>
      <CardHeader>
        <CardTitle>Agents</CardTitle>
        <CardDescription>
          Each agent in the pipeline — expand for its input, output, and audit trail.
        </CardDescription>
      </CardHeader>
      <CardContent className="space-y-2">
        {definitionsQuery.isPending || auditQuery.isPending ? (
          <Skeleton className="h-24 w-full" />
        ) : participants.length === 0 ? (
          <p className="text-sm text-muted-foreground">
            {run.phase === 'initializing'
              ? 'The run is still initializing — agents appear as they start.'
              : 'No agent activity recorded for this run.'}
          </p>
        ) : (
          <>
            <SectionHeading title="Pipeline agents" count={participants.length} />
            {participants.map((agent) => (
              <AgentInspector key={agent.name} agent={agent} results={results} audit={audit} />
            ))}
          </>
        )}
      </CardContent>
    </Card>
  )
}
