import { describe, expect, test } from 'vitest'

import {
  agentsSetupResponseSchema,
  llmConnectionDetailSchema,
  llmModelInventorySchema,
  putAgentsSetupRequestSchema,
  putLlmConnectionRequestSchema,
} from '../../src/api/schemas/agents-setup'
import { makeAgentsSetup } from '../helpers/fixtures'

const MODEL = {
  schema_version: 'llm_provider_model@1',
  provider: 'openai',
  model_id: 'gpt-5.4-mini',
  display_name: 'GPT-5.4 Mini',
  supported_tiers: ['fast', 'reasoning'],
  catalog_version: 'llm-provider-catalog@2',
  data_residency: 'global',
  allowed_data_classifications: [
    'public',
    'internal',
    'confidential',
    'restricted',
  ],
  endpoint_host: 'api.openai.com',
  input_cost_per_million_tokens_usd_micros: 250_000,
  output_cost_per_million_tokens_usd_micros: 1_000_000,
  pricing_status: 'catalog_reviewed',
} as const

describe('Agents setup strict schemas', () => {
  test('accepts the canonical active setup response', () => {
    const parsed = agentsSetupResponseSchema.parse(makeAgentsSetup())
    expect(parsed.analysis_ready).toBe(true)
    expect(parsed.assignments.map((assignment) => assignment.tier)).toEqual([
      'fast',
      'reasoning',
    ])
  })

  test('rejects response aliases and inconsistent readiness or authority', () => {
    expect(
      agentsSetupResponseSchema.safeParse({
        ...makeAgentsSetup(),
        default_model: 'gpt-5.4-mini',
        api_key: 'must-never-be-returned',
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...makeAgentsSetup(),
        analysis_ready: false,
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...makeAgentsSetup(),
        caller_capabilities: {
          ...makeAgentsSetup().caller_capabilities,
          can_activate: true,
        },
      }).success,
    ).toBe(false)
  })

  test('requires current assignment freshness and both version dimensions', () => {
    const setup = makeAgentsSetup()
    const assignment = { ...setup.assignments[0] }
    delete (assignment as Partial<typeof assignment>).current
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        assignments: [assignment, setup.assignments[1]],
      }).success,
    ).toBe(false)

    expect(
      putAgentsSetupRequestSchema.safeParse({
        project_id: 'demo',
        fast_model: {
          provider: 'openai',
          model: 'gpt-5.4-mini',
          connection_version: 1,
        },
        reasoning_model: {
          provider: 'openai',
          model: 'o4-mini',
          connection_version: 1,
          inventory_version: 2,
        },
        version: 0,
      }).success,
    ).toBe(false)
  })

  test('rejects impossible ready state, budget, connection, and timestamp combinations', () => {
    const setup = makeAgentsSetup()
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        assignments: [],
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        policy: {
          ...setup.policy,
          run_cost_limit_usd_micros: 0,
        },
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        connections: [
          {
            ...setup.connections[0],
            inventory_version: 2,
          },
        ],
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        activated_at: null,
      }).success,
    ).toBe(false)
    expect(
      agentsSetupResponseSchema.safeParse({
        ...setup,
        state: 'inactive',
        analysis_ready: false,
        blockers: ['project_inactive'],
        deactivated_at: '2026-07-30T13:00:00+00:00',
        deactivation_reason: null,
      }).success,
    ).toBe(false)
  })

  test('accepts exact catalog-v2 connection and inventory payloads', () => {
    const connection = {
      schema_version: 'llm_provider_connection@1',
      project_id: 'demo',
      provider: 'openai',
      version: 2,
      inventory_version: 3,
      state: 'active',
      catalog_version: 'llm-provider-catalog@2',
      validated_at: '2026-07-30T12:00:00+00:00',
      created_at: '2026-07-29T12:00:00+00:00',
      updated_at: '2026-07-30T12:00:00+00:00',
      revoked_at: null,
      model_count: 1,
      models: [MODEL],
    } as const
    expect(llmConnectionDetailSchema.parse(connection).inventory_version).toBe(
      3,
    )
    expect(
      llmModelInventorySchema.parse({
        schema_version: 'llm_provider_model_inventory@1',
        project_id: 'demo',
        provider: 'openai',
        connection_version: 2,
        inventory_version: 3,
        models: [MODEL],
      }).models[0]?.pricing_status,
    ).toBe('catalog_reviewed')
  })

  test('rejects unknown provider-key request fields', () => {
    expect(
      putLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        api_key: 'secret',
        version: 0,
        default_variant: 'legacy',
      }).success,
    ).toBe(false)
  })
})
