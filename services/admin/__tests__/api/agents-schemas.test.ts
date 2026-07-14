// Agents API zod mirrors: the approval response must tolerate a backend still
// on the old envelope, and the approval request must enforce exactly-one-of.
import { describe, expect, test } from 'vitest'

import {
  approvalRequestSchema,
  approvalResponseSchema,
  customAgentSpecSchema,
} from '../../src/api/schemas/agents'

describe('approvalResponseSchema', () => {
  test('accepts the full current envelope', () => {
    const parsed = approvalResponseSchema.parse({
      run_id: 'run-1',
      status: 'approved',
      approved_count: 1,
      rejected_count: 0,
      forked_runs: ['run-2'],
      opened_changesets: ['cs_1'],
      errors: ['experiment deploy failed: exp_checkout'],
      message: 'ok',
    })
    expect(parsed.approved_count).toBe(1)
    expect(parsed.opened_changesets).toEqual(['cs_1'])
    expect(parsed.errors).toEqual(['experiment deploy failed: exp_checkout'])
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

describe('customAgentSpecSchema', () => {
  const validSpec = {
    slug: 'churn_watch',
    display_name: 'Churn watch',
    description: '',
    system_prompt: 'Analyse churn.',
    user_prompt_template: 'Review {project_id}.',
    model_tier: 'fast',
    tools: [],
    preset_tools: [],
    requires: [],
    produces: 'churn_signals',
    memory_query: null,
    memory_top_k: 5,
    pipeline_order: 60,
    max_tool_steps: 8,
  }

  test('rejects whitespace-only prompts while preserving exact empty tools', () => {
    expect(customAgentSpecSchema.safeParse(validSpec).success).toBe(true)
    expect(
      customAgentSpecSchema.safeParse({ ...validSpec, system_prompt: ' \n\t ' }).success,
    ).toBe(false)
    expect(
      customAgentSpecSchema.safeParse({ ...validSpec, user_prompt_template: '   ' }).success,
    ).toBe(false)
  })
})
