import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'

import {
  getCodegenLlmModels,
  listCodegenLlmConnections,
  putCodegenLlmConnection,
  refreshCodegenLlmModels,
  revokeCodegenLlmConnection,
} from '../../src/api/codegen'
import {
  codegenLlmConnectionDetailSchema,
  codegenLlmConnectionListSchema,
  codegenLlmConnectionSummarySchema,
  codegenLlmModelInventorySchema,
  putCodegenLlmConnectionRequestSchema,
  refreshCodegenLlmConnectionRequestSchema,
  revokeCodegenLlmConnectionRequestSchema,
} from '../../src/api/schemas/codegen-llm-connections'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const connection = {
  baseUrl: 'http://codegen.test',
  actor: 'tester',
}

const OPENAI_MODEL = {
  schema_version: 'codegen_provider_model@1',
  provider: 'openai',
  model_id: 'gpt-5.4',
  display_name: 'GPT-5.4',
  supported_roles: ['editor', 'helper'],
  catalog_version: 'codegen-provider-catalog@1',
  context_window_tokens: 1_000_000,
  supports_tool_calling: true,
  supports_structured_output: true,
  data_residency: 'global',
  allowed_data_classifications: ['public', 'internal', 'confidential'],
  input_cost_per_million_tokens_usd_micros: 2_500_000,
  output_cost_per_million_tokens_usd_micros: 15_000_000,
  pricing_status: 'catalog_reviewed',
} as const

const OPENAI_SUMMARY = {
  schema_version: 'codegen_provider_connection@1',
  project_id: 'demo',
  provider: 'openai',
  version: 3,
  inventory_version: 2,
  state: 'active',
  catalog_version: 'codegen-provider-catalog@1',
  validated_at: '2026-08-01T12:00:00+00:00',
  created_at: '2026-08-01T11:00:00+00:00',
  updated_at: '2026-08-01T12:00:00+00:00',
  revoked_at: null,
  model_count: 1,
} as const

const OPENAI_DETAIL = {
  ...OPENAI_SUMMARY,
  models: [OPENAI_MODEL],
} as const

const OPENAI_INVENTORY = {
  schema_version: 'codegen_provider_model_inventory@1',
  project_id: 'demo',
  provider: 'openai',
  connection_version: 3,
  inventory_version: 2,
  models: [OPENAI_MODEL],
} as const

const REVOKED_OPENAI_SUMMARY = {
  ...OPENAI_SUMMARY,
  version: 4,
  inventory_version: 3,
  state: 'revoked',
  updated_at: '2026-08-01T13:00:00+00:00',
  revoked_at: '2026-08-01T13:00:00+00:00',
  model_count: 0,
} as const

describe('Codegen project LLM connection response schemas', () => {
  test('accept canonical response contracts and enforce cross-field invariants', () => {
    expect(codegenLlmConnectionSummarySchema.safeParse(OPENAI_SUMMARY).success).toBe(true)
    expect(codegenLlmConnectionDetailSchema.safeParse(OPENAI_DETAIL).success).toBe(true)
    expect(
      codegenLlmConnectionListSchema.safeParse({
        schema_version: 'codegen_provider_connection_list@1',
        project_id: 'demo',
        connections: [OPENAI_SUMMARY],
      }).success,
    ).toBe(true)
    expect(codegenLlmModelInventorySchema.safeParse(OPENAI_INVENTORY).success).toBe(true)
    expect(
      codegenLlmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        model_count: 2,
      }).success,
    ).toBe(false)
    expect(
      codegenLlmConnectionListSchema.safeParse({
        schema_version: 'codegen_provider_connection_list@1',
        project_id: 'demo',
        connections: [OPENAI_SUMMARY, OPENAI_SUMMARY],
      }).success,
    ).toBe(false)
  })

  test.each([
    ['api_key', 'sk-secret'],
    ['credential_id', 'credential-1'],
    ['ciphertext', 'ciphertext'],
    ['nonce', 'nonce'],
    ['internal_model_id', 'openai/gpt-5.4'],
    ['raw_provider_payload', { data: [] }],
  ])('rejects accidental secret or internal response field %s', (field, value) => {
    expect(
      codegenLlmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        [field]: value,
      }).success,
    ).toBe(false)
    expect(
      codegenLlmModelInventorySchema.safeParse({
        ...OPENAI_INVENTORY,
        models: [{ ...OPENAI_MODEL, [field]: value }],
      }).success,
    ).toBe(false)
  })

  test('rejects noncanonical providers, malformed metadata, and duplicate metadata', () => {
    expect(
      codegenLlmConnectionSummarySchema.safeParse({
        ...OPENAI_SUMMARY,
        provider: 'OpenAI',
      }).success,
    ).toBe(false)
    expect(
      codegenLlmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        models: [
          {
            ...OPENAI_MODEL,
            supported_roles: ['editor', 'editor'],
          },
        ],
      }).success,
    ).toBe(false)
    expect(
      codegenLlmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        models: [
          {
            ...OPENAI_MODEL,
            input_cost_per_million_tokens_usd_micros: -1,
          },
        ],
      }).success,
    ).toBe(false)
  })
})

describe('Codegen project LLM connection request schemas', () => {
  test('accept only the exact canonical mutation bodies', () => {
    expect(
      putCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        api_key: 'sk-project',
        version: 0,
      }).success,
    ).toBe(true)
    expect(
      refreshCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        version: 3,
      }).success,
    ).toBe(true)
    expect(
      revokeCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        version: 3,
        reason: 'Credential rotated',
      }).success,
    ).toBe(true)
    expect(
      putCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        provider: 'openai',
        api_key: 'sk-project',
        version: 0,
      }).success,
    ).toBe(false)
    expect(
      refreshCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        version: 3,
        refresh: true,
      }).success,
    ).toBe(false)
    expect(
      revokeCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        version: 3,
        reason: ' Credential rotated ',
      }).success,
    ).toBe(false)
  })

  test('bounds opaque API keys by UTF-8 bytes', () => {
    expect(
      putCodegenLlmConnectionRequestSchema.safeParse({
        project_id: 'demo',
        api_key: 'é'.repeat(8_193),
        version: 0,
      }).success,
    ).toBe(false)
  })
})

describe('Codegen project LLM connection client', () => {
  test('uses the exact endpoint paths, query parameters, and mutation bodies', async () => {
    const seenUrls: string[] = []
    const seenBodies: unknown[] = []

    server.use(
      http.get('http://codegen.test/v1/llm-connections', ({ request }) => {
        seenUrls.push(request.url)
        return HttpResponse.json({
          schema_version: 'codegen_provider_connection_list@1',
          project_id: 'demo',
          connections: [OPENAI_SUMMARY],
        })
      }),
      http.put('http://codegen.test/v1/llm-connections/openai', async ({ request }) => {
        seenUrls.push(request.url)
        seenBodies.push(await request.json())
        return HttpResponse.json(OPENAI_DETAIL)
      }),
      http.get(
        'http://codegen.test/v1/llm-connections/openai/models',
        ({ request }) => {
          seenUrls.push(request.url)
          return HttpResponse.json(OPENAI_INVENTORY)
        },
      ),
      http.post(
        'http://codegen.test/v1/llm-connections/openai/refresh-models',
        async ({ request }) => {
          seenUrls.push(request.url)
          seenBodies.push(await request.json())
          return HttpResponse.json(OPENAI_DETAIL)
        },
      ),
      http.post(
        'http://codegen.test/v1/llm-connections/openai/revoke',
        async ({ request }) => {
          seenUrls.push(request.url)
          seenBodies.push(await request.json())
          return HttpResponse.json(REVOKED_OPENAI_SUMMARY)
        },
      ),
    )

    await listCodegenLlmConnections(connection, 'demo')
    await putCodegenLlmConnection(connection, 'openai', {
      project_id: 'demo',
      api_key: 'sk-project-secret',
      version: 0,
    })
    await getCodegenLlmModels(connection, 'openai', 'demo')
    await refreshCodegenLlmModels(connection, 'openai', {
      project_id: 'demo',
      version: 3,
    })
    await revokeCodegenLlmConnection(connection, 'openai', {
      project_id: 'demo',
      version: 3,
      reason: 'Credential rotated',
    })

    expect(seenUrls).toEqual([
      'http://codegen.test/v1/llm-connections?project_id=demo',
      'http://codegen.test/v1/llm-connections/openai',
      'http://codegen.test/v1/llm-connections/openai/models?project_id=demo',
      'http://codegen.test/v1/llm-connections/openai/refresh-models',
      'http://codegen.test/v1/llm-connections/openai/revoke',
    ])
    expect(seenBodies).toEqual([
      {
        project_id: 'demo',
        api_key: 'sk-project-secret',
        version: 0,
      },
      {
        project_id: 'demo',
        version: 3,
      },
      {
        project_id: 'demo',
        version: 3,
        reason: 'Credential rotated',
      },
    ])
  })

  test('rejects responses that cross project or provider authority', async () => {
    const anthropicModel = {
      ...OPENAI_MODEL,
      provider: 'anthropic',
      model_id: 'claude-sonnet-4-6',
      display_name: 'Claude Sonnet 4.6',
    } as const

    server.use(
      http.get('http://codegen.test/v1/llm-connections', () =>
        HttpResponse.json({
          schema_version: 'codegen_provider_connection_list@1',
          project_id: 'other',
          connections: [],
        }),
      ),
      http.put('http://codegen.test/v1/llm-connections/openai', () =>
        HttpResponse.json({
          ...OPENAI_DETAIL,
          provider: 'anthropic',
          models: [anthropicModel],
        }),
      ),
      http.get('http://codegen.test/v1/llm-connections/openai/models', () =>
        HttpResponse.json({
          ...OPENAI_INVENTORY,
          provider: 'anthropic',
          models: [anthropicModel],
        }),
      ),
      http.post(
        'http://codegen.test/v1/llm-connections/openai/refresh-models',
        () => HttpResponse.json({ ...OPENAI_DETAIL, project_id: 'other' }),
      ),
      http.post('http://codegen.test/v1/llm-connections/openai/revoke', () =>
        HttpResponse.json({ ...REVOKED_OPENAI_SUMMARY, provider: 'anthropic' }),
      ),
    )

    await expect(listCodegenLlmConnections(connection, 'demo')).rejects.toThrow(
      'Codegen LLM connection list crossed project authority',
    )
    await expect(
      putCodegenLlmConnection(connection, 'openai', {
        project_id: 'demo',
        api_key: 'sk-secret',
        version: 0,
      }),
    ).rejects.toThrow('Codegen LLM connection response crossed project authority')
    await expect(getCodegenLlmModels(connection, 'openai', 'demo')).rejects.toThrow(
      'Codegen LLM model inventory crossed project authority',
    )
    await expect(
      refreshCodegenLlmModels(connection, 'openai', {
        project_id: 'demo',
        version: 3,
      }),
    ).rejects.toThrow('Codegen LLM model refresh crossed project authority')
    await expect(
      revokeCodegenLlmConnection(connection, 'openai', {
        project_id: 'demo',
        version: 3,
        reason: 'Credential rotated',
      }),
    ).rejects.toThrow('Codegen LLM connection revocation crossed project authority')
  })

  test('does not treat provider credential rejection as an expired Admin session', async () => {
    const unauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    server.use(
      http.put('http://codegen.test/v1/llm-connections/openai', () =>
        HttpResponse.json(
          {
            detail: {
              code: 'invalid_key',
              message: 'Provider rejected the credential',
            },
          },
          { status: 401 },
        ),
      ),
    )

    try {
      await expect(
        putCodegenLlmConnection(connection, 'openai', {
          project_id: 'demo',
          api_key: 'invalid-provider-key',
          version: 0,
        }),
      ).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    }
  })
})
