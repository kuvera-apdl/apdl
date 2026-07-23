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
  autonomousMutationsEnabled = true,
): GateOutcome {
  if (autonomyLevel <= 1) return 'halt'
  if (alwaysRequireApproval) return 'approve'
  if (!autonomousMutationsEnabled) return 'approve'
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
  {
    level: 3,
    label: 'L3',
    summary: 'When operator-enabled, low-risk actions auto-deploy; medium/high require approval.',
  },
  {
    level: 4,
    label: 'L4',
    summary:
      'When operator-enabled, safety-passing actions auto-deploy except those explicitly always-gated.',
  },
]

export const MATRIX_ROWS: {
  label: string
  outcomes: (level: number, autonomousMutationsEnabled?: boolean) => GateOutcome
}[] = [
  { label: 'Failed safety check', outcomes: () => 'halt' },
  {
    label: 'Passed, low risk',
    outcomes: (level, enabled = true) => gateOutcome(level, 'low', false, enabled),
  },
  {
    label: 'Passed, medium/high risk',
    outcomes: (level, enabled = true) => gateOutcome(level, 'medium', false, enabled),
  },
  {
    label: 'Feature proposals (always gated)',
    outcomes: (level, enabled = true) => gateOutcome(level, 'low', true, enabled),
  },
]
