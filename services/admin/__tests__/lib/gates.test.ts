// The shared gate→decision transform behind the Decide surface.
import { describe, expect, test } from 'vitest'

import type { RunResults, RunStatus } from '../../src/api/types/agents'
import { decisionsForRun } from '../../src/lib/gates'

function run(overrides: Partial<RunStatus>): RunStatus {
  return {
    run_id: 'r1',
    project_id: 'demo',
    status: 'waiting_approval',
    phase: 'experiment_design_approval',
    insights_count: 0,
    experiments_count: 0,
    started_at: '2026-07-08T00:00:00Z',
    updated_at: '2026-07-08T00:00:00Z',
    ...overrides,
  }
}

function results(overrides: Partial<RunResults>): RunResults {
  return {
    run_id: 'r1',
    insights: [],
    experiment_designs: [],
    personalizations: [],
    feature_proposals: [],
    changesets: [],
    ...overrides,
  }
}

test('a non-waiting run yields no decisions', () => {
  expect(decisionsForRun(run({ status: 'running' }), results({}))).toEqual([])
})

test('missing results yields no decisions', () => {
  expect(decisionsForRun(run({}), null)).toEqual([])
})

test('experiment_design gate → a "run the experiment?" question with evidence', () => {
  const decisions = decisionsForRun(
    run({ phase: 'experiment_design_approval' }),
    results({
      experiment_designs: [
        {
          experiment_id: 'exp_cta',
          hypothesis: 'A sticky CTA lifts signups.',
          primary_metric: { event: 'signup' },
          variants: [{ key: 'control' }, { key: 'treatment' }],
          flag_config: { key: 'exp_cta', fallthrough: { rollout: { percentage: 20 } } },
        },
      ],
    }),
  )
  expect(decisions).toHaveLength(1)
  const d = decisions[0]!
  expect(d.itemId).toBe('exp_cta')
  expect(d.agent).toBe('experiment_design')
  expect(d.stage).toBe('awaiting_approval')
  expect(d.question).toContain('exp_cta')
  expect(d.detail).toContain('sticky CTA')
  expect(d.evidence).toContainEqual({ label: 'variants', value: 2 })
  expect(d.evidence).toContainEqual({ label: 'primary metric', value: 'signup' })
  expect(d.evidence).toContainEqual({ label: 'traffic', value: '20%' })
})

test('feature_proposal gate → a "make permanent?" ship decision', () => {
  const decisions = decisionsForRun(
    run({ phase: 'feature_proposal_approval' }),
    results({
      feature_proposals: [
        {
          proposal_id: 'feat_dark',
          title: 'Dark mode',
          source_experiment_id: 'exp_dark',
          problem_statement: 'Users want dark mode.',
          evidence: { metrics: { effect_size: '+18%', p_value: 0.003 } },
        },
      ],
    }),
  )
  const d = decisions[0]!
  expect(d.itemId).toBe('feat_dark')
  expect(d.stage).toBe('ship')
  expect(d.question).toContain('Dark mode')
  expect(d.evidence).toContainEqual({ label: 'effect', value: '+18%', tone: 'success' })
  expect(d.evidence).toContainEqual({ label: 'from', value: 'exp_dark' })
})

test('positional fallback id for an unkeyed item', () => {
  const decisions = decisionsForRun(
    run({ phase: 'feature_proposal_approval' }),
    results({ feature_proposals: [{ title: 'No id' }] }),
  )
  expect(decisions[0]!.itemId).toBe('__index_0')
})
