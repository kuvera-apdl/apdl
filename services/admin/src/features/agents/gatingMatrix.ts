// UI copy of the autonomy gate (services/agents/app/framework/gating.py).
// gating-matrix.test.ts asserts these tables against the gate's exact
// semantics so the rendered copy can never drift from the code.
export type GateOutcome = 'halt' | 'approve' | 'deploy'

export type RiskLevel = 'low' | 'medium' | 'high'

/** Mirror of gate_action() for a PASSING safety check, by level and risk. */
export function gateOutcome(
  autonomyLevel: number,
  risk: RiskLevel,
  alwaysRequireApproval = false,
): GateOutcome {
  if (autonomyLevel <= 1) return 'halt'
  if (alwaysRequireApproval) return 'approve'
  if (autonomyLevel >= 4) return 'deploy'
  if (autonomyLevel >= 3 && risk === 'low') return 'deploy'
  if (autonomyLevel >= 2) return 'approve'
  return 'halt'
}

export interface AutonomyLevelDef {
  level: number
  label: string
  summary: string
  recommended?: boolean
}

export const AUTONOMY_LEVELS: AutonomyLevelDef[] = [
  { level: 1, label: 'L1', summary: 'Suggest only — nothing deploys, even after passing safety.' },
  {
    level: 2,
    label: 'L2',
    summary: 'Every passing action is held for your approval.',
    recommended: true,
  },
  { level: 3, label: 'L3', summary: 'Low-risk actions auto-deploy; medium/high held for approval.' },
  {
    level: 4,
    label: 'L4',
    summary: 'Every safety-passing action auto-deploys except actions explicitly marked always-gated.',
  },
]

export const MATRIX_ROWS: { label: string; outcomes: (level: number) => GateOutcome }[] = [
  { label: 'Failed safety check', outcomes: () => 'halt' },
  { label: 'Passed, low risk', outcomes: (level) => gateOutcome(level, 'low') },
  { label: 'Passed, medium/high risk', outcomes: (level) => gateOutcome(level, 'medium') },
  { label: 'Feature proposals (always gated)', outcomes: (level) => gateOutcome(level, 'low', true) },
]
