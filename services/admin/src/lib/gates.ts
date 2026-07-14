// Turn a waiting run + its persisted results into question-phrased decisions
// with evidence (admin-console-purpose-ia.md → Decide). Shared so the Decide
// surface and the run monitor describe a gate identically instead of each
// re-deriving item ids and phrasing.

import type { Evidence } from '@/components/shared/EvidenceRow'
import type { LoopStage } from '@/lib/loopStatus'
import type { RunResults, RunStatus } from '@/api/types/agents'

// Gated agent → the results key its pending items live under.
const GATE_RESULT_KEY = {
  experiment_design: 'experiment_designs',
  feature_proposal: 'feature_proposals',
  personalization: 'personalizations',
  code_implementation: 'changesets',
} as const

type GatedAgent = keyof typeof GATE_RESULT_KEY

export interface Decision {
  runId: string
  itemId: string
  agent: GatedAgent
  stage: LoopStage
  question: string
  detail?: string
  evidence: Evidence[]
}

function rec(item: unknown): Record<string, unknown> {
  return typeof item === 'object' && item !== null ? (item as Record<string, unknown>) : {}
}

function str(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function num(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

// Mirrors the server's _item_id (routers/approvals.py): experiment_id or
// flag_config.key for designs, proposal_id otherwise; positional fallback for a
// lone unkeyed item.
function itemId(item: Record<string, unknown>, agent: GatedAgent, index: number): string {
  const flag = rec(item.flag_config)
  const raw =
    agent === 'experiment_design' ? str(item.experiment_id) || str(flag.key) : str(item.proposal_id)
  return raw.trim() !== '' ? raw : `__index_${index}`
}

function designDecision(runId: string, item: Record<string, unknown>, index: number): Decision {
  const id = itemId(item, 'experiment_design', index)
  const metric = rec(item.primary_metric)
  const variants = Array.isArray(item.variants) ? item.variants.length : null
  const evidence: Evidence[] = []
  if (variants) evidence.push({ label: 'variants', value: variants })
  if (str(metric.event)) evidence.push({ label: 'primary metric', value: str(metric.event) })
  const rollout = rec(rec(rec(item.flag_config).fallthrough).rollout)
  const traffic = num(rollout.percentage)
  if (traffic !== null) evidence.push({ label: 'traffic', value: `${traffic}%` })
  return {
    runId,
    itemId: id,
    agent: 'experiment_design',
    stage: 'awaiting_approval',
    question: `Run the "${str(item.experiment_id) || id}" experiment?`,
    detail: str(item.hypothesis) || str(item.description) || undefined,
    evidence,
  }
}

function proposalDecision(runId: string, item: Record<string, unknown>, index: number): Decision {
  const id = itemId(item, 'feature_proposal', index)
  const evidenceObj = rec(item.evidence)
  const metrics = rec(evidenceObj.metrics)
  const evidence: Evidence[] = []
  const effect = metrics.effect_size ?? metrics.lift
  if (effect != null) evidence.push({ label: 'effect', value: String(effect), tone: 'success' })
  if (metrics.p_value != null) evidence.push({ label: 'p', value: String(metrics.p_value) })
  if (str(item.source_experiment_id))
    evidence.push({ label: 'from', value: str(item.source_experiment_id) })
  return {
    runId,
    itemId: id,
    agent: 'feature_proposal',
    stage: 'ship',
    question: `Make "${str(item.title) || id}" a permanent feature?`,
    detail: str(item.problem_statement) || str(item.proposed_solution) || undefined,
    evidence,
  }
}

function changesetDecision(runId: string, item: Record<string, unknown>, index: number): Decision {
  const id = itemId(item, 'code_implementation', index)
  return {
    runId,
    itemId: id,
    agent: 'code_implementation',
    stage: 'building',
    question: `Open a pull request for "${str(item.title) || id}"?`,
    detail: str(item.spec) ? `${str(item.spec).slice(0, 160)}…` : undefined,
    evidence: [],
  }
}

// Every pending decision a single waiting run holds. Non-waiting runs and runs
// whose gate has no persisted payload yield [].
export function decisionsForRun(run: RunStatus, results: RunResults | null): Decision[] {
  if (run.status !== 'waiting_approval' || !results) return []
  const agent = run.phase.replace(/_approval$/, '') as GatedAgent
  const key = GATE_RESULT_KEY[agent]
  if (!key) return []
  const items = (results[key] as unknown[]) ?? []
  return items.map((raw, index) => {
    const item = rec(raw)
    if (agent === 'experiment_design') return designDecision(run.run_id, item, index)
    if (agent === 'feature_proposal') return proposalDecision(run.run_id, item, index)
    if (agent === 'code_implementation') return changesetDecision(run.run_id, item, index)
    // personalization (parked) or unknown — a minimal generic decision.
    return {
      runId: run.run_id,
      itemId: itemId(item, agent, index),
      agent,
      stage: 'awaiting_approval',
      question: `Approve ${agent.replace(/_/g, ' ')} item?`,
      evidence: [],
    }
  })
}
