// Agents API zod mirrors: the approval response must tolerate a backend still
// on the old envelope, and the approval request must enforce exactly-one-of.
import { describe, expect, test } from 'vitest'

import { approvalRequestSchema, approvalResponseSchema } from '../../src/api/schemas/agents'

describe('approvalResponseSchema', () => {
  test('accepts the full current envelope', () => {
    const parsed = approvalResponseSchema.parse({
      run_id: 'run-1',
      status: 'approved',
      approved_count: 1,
      rejected_count: 0,
      forked_runs: ['run-2'],
      opened_changesets: ['cs_1'],
      message: 'ok',
    })
    expect(parsed.approved_count).toBe(1)
    expect(parsed.opened_changesets).toEqual(['cs_1'])
  })

  test('tolerates the legacy { run_id, status, message } envelope', () => {
    // A backend not yet updated returns only the original fields — this must
    // parse (not schema_mismatch) so a successful approval is not reported failed.
    // The count/array fields are left undefined; the UI coalesces them.
    const res = approvalResponseSchema.safeParse({
      run_id: 'run-1',
      status: 'approved',
      message: 'ok',
    })
    expect(res.success).toBe(true)
    expect(res.data?.approved_count).toBeUndefined()
    expect(res.data?.forked_runs).toBeUndefined()
  })
})

describe('approvalRequestSchema', () => {
  test('accepts exactly one of decisions / approved', () => {
    expect(approvalRequestSchema.safeParse({ approved: true }).success).toBe(true)
    expect(
      approvalRequestSchema.safeParse({ decisions: [{ item_id: 'p1', approved: true }] }).success,
    ).toBe(true)
  })

  test('rejects neither and both (mirrors the server 400)', () => {
    expect(approvalRequestSchema.safeParse({ comment: 'hi' }).success).toBe(false)
    expect(
      approvalRequestSchema.safeParse({ approved: true, decisions: [] }).success,
    ).toBe(false)
  })
})
