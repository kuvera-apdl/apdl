// Agents API zod mirrors: approval requests have one strict per-item shape.
import { describe, expect, test } from 'vitest'

import {
  approvalRequestSchema,
  approvalResponseSchema,
  customAgentSpecSchema,
} from '../../src/api/schemas/agents'

describe('approvalResponseSchema', () => {
  test('accepts the strict durable command envelope', () => {
    const parsed = approvalResponseSchema.parse({
      command_id: '018f3d4e-c1c2-7000-8000-000000000001',
      run_id: 'run-1',
      actor_credential_id: 'test-agents',
      actor_user_id: '20000000-0000-4000-8000-000000000002',
      gate_id: 'run-1:experiment_design',
      gate_agent: 'experiment_design',
      status: 'queued',
      approved_count: 1,
      rejected_count: 0,
      comment: null,
      last_error: null,
      created_at: '2026-07-15T00:00:00Z',
      updated_at: '2026-07-15T00:00:00Z',
      effects: [
        {
          effect_id: '018f3d4e-c1c2-7000-8000-000000000002',
          item_id: 'exp_checkout',
          effect_type: 'stage_experiment_draft',
          status: 'queued',
          attempt_count: 0,
          last_error: null,
          result: null,
        },
      ],
    })
    expect(parsed.approved_count).toBe(1)
    expect(parsed.actor_user_id).toBe('20000000-0000-4000-8000-000000000002')
    expect(parsed.effects[0]?.effect_type).toBe('stage_experiment_draft')
  })

  test('rejects legacy and unknown response shapes', () => {
    const res = approvalResponseSchema.safeParse({
      run_id: 'run-1',
      status: 'approved',
      message: 'ok',
    })
    expect(res.success).toBe(false)
  })
})

describe('approvalRequestSchema', () => {
  test('accepts canonical unique per-item decisions', () => {
    expect(
      approvalRequestSchema.safeParse({ decisions: [{ item_id: 'p1', approved: true }] }).success,
    ).toBe(true)
  })

  test('rejects missing, empty, legacy, duplicate, and unknown fields', () => {
    expect(approvalRequestSchema.safeParse({ comment: 'hi' }).success).toBe(false)
    expect(approvalRequestSchema.safeParse({ decisions: [] }).success).toBe(false)
    expect(approvalRequestSchema.safeParse({ approved: true }).success).toBe(false)
    expect(
      approvalRequestSchema.safeParse({
        decisions: [
          { item_id: 'p1', approved: false },
          { item_id: 'p1', approved: true },
        ],
      }).success,
    ).toBe(false)
    expect(
      approvalRequestSchema.safeParse({
        decisions: [{ item_id: 'p1', approved: true, unexpected: true }],
      }).success,
    ).toBe(false)
  })

  test('bounds identifiers and comments', () => {
    expect(
      approvalRequestSchema.safeParse({ decisions: [{ item_id: ' p1 ', approved: true }] }).success,
    ).toBe(false)
    expect(
      approvalRequestSchema.safeParse({
        decisions: [{ item_id: 'p1', approved: true }],
        comment: 'x'.repeat(2_001),
      }).success,
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
