// Browser-local run history — and the prune that stops a deleted run from
// lingering as a ghost (stale last_status) after the server 404s it.
import { beforeEach, describe, expect, test } from 'vitest'

import {
  loadTrackedRuns,
  removeTrackedRun,
  trackRun,
  updateTrackedRunStatus,
  type TrackedRun,
} from '../../src/features/agents/runHistory'

const WS = 'ws-1'

function run(id: string, status = 'started'): TrackedRun {
  return {
    run_id: id,
    triggered_at: '2026-07-08T00:00:00Z',
    last_status: status,
    autonomy_level: 2,
    analysis_types: ['behavior_analysis'],
  }
}

describe('runHistory', () => {
  beforeEach(() => localStorage.clear())

  test('track then load returns newest first', () => {
    trackRun(WS, run('a'))
    trackRun(WS, run('b'))
    expect(loadTrackedRuns(WS).map((r) => r.run_id)).toEqual(['b', 'a'])
  })

  test('updateTrackedRunStatus mutates the matching run only', () => {
    trackRun(WS, run('a', 'started'))
    updateTrackedRunStatus(WS, 'a', 'waiting_approval')
    expect(loadTrackedRuns(WS)[0]!.last_status).toBe('waiting_approval')
  })

  test('removeTrackedRun drops the run so a stale status cannot ghost it', () => {
    trackRun(WS, run('a', 'waiting_approval'))
    trackRun(WS, run('b'))
    removeTrackedRun(WS, 'a')
    expect(loadTrackedRuns(WS).map((r) => r.run_id)).toEqual(['b'])
  })

  test('removeTrackedRun is a no-op for an unknown id and other workspaces', () => {
    trackRun(WS, run('a'))
    removeTrackedRun(WS, 'missing')
    removeTrackedRun('other-ws', 'a')
    expect(loadTrackedRuns(WS).map((r) => r.run_id)).toEqual(['a'])
  })
})
