// The shared loop status vocabulary — the mapping every loop surface relies on.
import { describe, expect, test } from 'vitest'

import { loopStageMeta, runToLoopStage, verdictToLoopStage } from '../../src/lib/loopStatus'

describe('runToLoopStage', () => {
  test('waiting_approval → awaiting_approval regardless of phase', () => {
    expect(runToLoopStage('waiting_approval', 'experiment_design_approval')).toBe('awaiting_approval')
  })

  test('running phase decides the stage', () => {
    expect(runToLoopStage('running', 'code_implementation')).toBe('building')
    expect(runToLoopStage('running', 'experiment_evaluation')).toBe('running')
    expect(runToLoopStage('running', 'behavior_analysis')).toBe('designing')
  })

  test('terminal statuses map to done/failed/rollback', () => {
    expect(runToLoopStage('completed', 'done')).toBe('done')
    expect(runToLoopStage('completed_with_errors', 'done')).toBe('failed')
    expect(runToLoopStage('failed', 'error')).toBe('failed')
    expect(runToLoopStage('rejected', 'x')).toBe('rollback')
  })

  test('unknown status degrades to designing, never throws', () => {
    expect(runToLoopStage('who_knows')).toBe('designing')
  })
})

describe('verdictToLoopStage', () => {
  test('maps every verdict', () => {
    expect(verdictToLoopStage('ship')).toBe('ship')
    expect(verdictToLoopStage('rollback')).toBe('rollback')
    expect(verdictToLoopStage('iterate')).toBe('iterate')
    expect(verdictToLoopStage('extend')).toBe('extend')
    expect(verdictToLoopStage('immature')).toBe('needs_data')
  })
})

describe('loopStageMeta', () => {
  test('every stage has a label and a tone', () => {
    expect(loopStageMeta('ship')).toEqual({ label: 'ship', tone: 'success' })
    expect(loopStageMeta('awaiting_approval').tone).toBe('warn')
    expect(loopStageMeta('rollback').tone).toBe('danger')
  })
})
