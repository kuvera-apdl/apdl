import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'

import {
  getLlmModels,
  listLlmConnections,
  llmConnectionDetailSchema,
  llmConnectionListSchema,
  llmConnectionSummarySchema,
  llmModelInventorySchema,
  putLlmConnection,
  refreshLlmModels,
  revokeLlmConnection,
} from '../../src/api/llmConnections'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const connection = {
  baseUrl: 'http://agents.test',
  actor: 'tester',
}

const OPENAI_MODEL = {
  schema_version: 'llm_provider_model@1',
  provider: 'openai',
  model_id: 'gpt-5',
  display_name: 'GPT-5',
  supported_tiers: ['fast', 'reasoning'],
  catalog_version: '2026-08-01',
  data_residency: 'us',
  allowed_data_classifications: ['public', 'internal'],
  pricing_status: 'operator_review_required',
} as const

const OPENAI_SUMMARY = {
  schema_version: 'llm_provider_connection@1',
  project_id: 'demo',
  provider: 'openai',
  version: 3,
  state: 'active',
  catalog_version: '2026-08-01',
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
  schema_version: 'llm_provider_model_inventory@1',
  project_id: 'demo',
  provider: 'openai',
  connection_version: 3,
  models: [OPENAI_MODEL],
} as const

const REVOKED_OPENAI_SUMMARY = {
  ...OPENAI_SUMMARY,
  version: 4,
  state: 'revoked',
  updated_at: '2026-08-01T13:00:00+00:00',
  revoked_at: '2026-08-01T13:00:00+00:00',
} as const

describe('project LLM connection response schemas', () => {
  test('accept canonical response contracts and reject unknown fields at every boundary', () => {
    expect(llmConnectionSummarySchema.safeParse(OPENAI_SUMMARY).success).toBe(true)
    expect(llmConnectionDetailSchema.safeParse(OPENAI_DETAIL).success).toBe(true)
    expect(
      llmConnectionListSchema.safeParse({
        schema_version: 'llm_provider_connection_list@1',
        project_id: 'demo',
        connections: [OPENAI_SUMMARY],
      }).success,
    ).toBe(true)
    expect(llmModelInventorySchema.safeParse(OPENAI_INVENTORY).success).toBe(true)

    expect(
      llmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        credential_source: 'environment',
      }).success,
    ).toBe(false)
    expect(
      llmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        models: [{ ...OPENAI_MODEL, deprecated: false }],
      }).success,
    ).toBe(false)
    expect(
      llmConnectionListSchema.safeParse({
        schema_version: 'llm_provider_connection_list@1',
        project_id: 'demo',
        connections: [{ ...OPENAI_SUMMARY, health: 'ready' }],
      }).success,
    ).toBe(false)
    expect(
      llmModelInventorySchema.safeParse({
        ...OPENAI_INVENTORY,
        next_page_token: null,
      }).success,
    ).toBe(false)
  })

  test('does not permit secret material in any connection response', async () => {
    expect(
      llmConnectionSummarySchema.safeParse({
        ...OPENAI_SUMMARY,
        api_key: 'sk-secret',
      }).success,
    ).toBe(false)
    expect(
      llmConnectionDetailSchema.safeParse({
        ...OPENAI_DETAIL,
        api_key: 'sk-secret',
      }).success,
    ).toBe(false)
    expect(
      llmModelInventorySchema.safeParse({
        ...OPENAI_INVENTORY,
        models: [{ ...OPENAI_MODEL, api_key: 'sk-secret' }],
      }).success,
    ).toBe(false)

    server.use(
      http.put('http://agents.test/v1/agents/llm-connections/openai', () =>
        HttpResponse.json({ ...OPENAI_DETAIL, api_key: 'sk-secret' }),
      ),
    )

    await expect(
      putLlmConnection(connection, 'openai', 'demo', 'sk-secret', 0),
    ).rejects.toMatchObject({ code: 'schema_mismatch' })
  })

  test.each([
    {
      label: 'connect',
      path: 'http://agents.test/v1/agents/llm-connections/openai',
      request: () =>
        putLlmConnection(connection, 'openai', 'demo', 'invalid-provider-key', 0),
    },
    {
      label: 'refresh',
      path: 'http://agents.test/v1/agents/llm-connections/openai/refresh-models',
      request: () => refreshLlmModels(connection, 'openai', 'demo', 3),
    },
  ])('does not treat provider credential rejection during $label as an expired Admin session', async ({
    path,
    request,
  }) => {
    const unauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    server.use(
      http.all(path, () =>
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
      await expect(request()).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    }
  })

  test.each([
    {
      label: 'connect',
      path: 'http://agents.test/v1/agents/llm-connections/openai',
      request: () =>
        putLlmConnection(connection, 'openai', 'demo', 'provider-key', 0),
    },
    {
      label: 'refresh',
      path: 'http://agents.test/v1/agents/llm-connections/openai/refresh-models',
      request: () => refreshLlmModels(connection, 'openai', 'demo', 3),
    },
  ])('still terminates an expired Admin session during $label', async ({ path, request }) => {
    const unauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    server.use(
      http.all(path, () =>
        HttpResponse.json({ detail: 'Login required' }, { status: 401 }),
      ),
    )

    try {
      await expect(request()).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).toHaveBeenCalledTimes(1)
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    }
  })
})

describe('project LLM connection API', () => {
  test('uses the exact endpoint paths, query parameters, and mutation bodies', async () => {
    const seenUrls: string[] = []
    const seenBodies: unknown[] = []

    server.use(
      http.get('http://agents.test/v1/agents/llm-connections', ({ request }) => {
        seenUrls.push(request.url)
        return HttpResponse.json({
          schema_version: 'llm_provider_connection_list@1',
          project_id: 'demo',
          connections: [OPENAI_SUMMARY],
        })
      }),
      http.put(
        'http://agents.test/v1/agents/llm-connections/openai',
        async ({ request }) => {
          seenUrls.push(request.url)
          seenBodies.push(await request.json())
          return HttpResponse.json(OPENAI_DETAIL)
        },
      ),
      http.get(
        'http://agents.test/v1/agents/llm-connections/openai/models',
        ({ request }) => {
          seenUrls.push(request.url)
          return HttpResponse.json(OPENAI_INVENTORY)
        },
      ),
      http.post(
        'http://agents.test/v1/agents/llm-connections/openai/refresh-models',
        async ({ request }) => {
          seenUrls.push(request.url)
          seenBodies.push(await request.json())
          return HttpResponse.json(OPENAI_DETAIL)
        },
      ),
      http.post(
        'http://agents.test/v1/agents/llm-connections/openai/revoke',
        async ({ request }) => {
          seenUrls.push(request.url)
          seenBodies.push(await request.json())
          return HttpResponse.json(REVOKED_OPENAI_SUMMARY)
        },
      ),
    )

    await listLlmConnections(connection, 'demo')
    await putLlmConnection(connection, 'openai', 'demo', 'sk-project-secret', 0)
    await getLlmModels(connection, 'openai', 'demo')
    await refreshLlmModels(connection, 'openai', 'demo', 3)
    await revokeLlmConnection(connection, 'openai', 'demo', 3, 'Credential rotated')

    expect(seenUrls).toEqual([
      'http://agents.test/v1/agents/llm-connections?project_id=demo',
      'http://agents.test/v1/agents/llm-connections/openai',
      'http://agents.test/v1/agents/llm-connections/openai/models?project_id=demo',
      'http://agents.test/v1/agents/llm-connections/openai/refresh-models',
      'http://agents.test/v1/agents/llm-connections/openai/revoke',
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

  test('rejects responses that cross the requested project or provider authority', async () => {
    const anthropicModel = {
      ...OPENAI_MODEL,
      provider: 'anthropic',
      model_id: 'claude-sonnet-4',
      display_name: 'Claude Sonnet 4',
    } as const
    const anthropicDetail = {
      ...OPENAI_DETAIL,
      provider: 'anthropic',
      models: [anthropicModel],
    } as const

    server.use(
      http.get('http://agents.test/v1/agents/llm-connections', () =>
        HttpResponse.json({
          schema_version: 'llm_provider_connection_list@1',
          project_id: 'other',
          connections: [],
        }),
      ),
      http.put('http://agents.test/v1/agents/llm-connections/openai', () =>
        HttpResponse.json(anthropicDetail),
      ),
      http.get('http://agents.test/v1/agents/llm-connections/openai/models', () =>
        HttpResponse.json({
          ...OPENAI_INVENTORY,
          provider: 'anthropic',
          models: [anthropicModel],
        }),
      ),
      http.post(
        'http://agents.test/v1/agents/llm-connections/openai/refresh-models',
        () =>
          HttpResponse.json({
            ...OPENAI_DETAIL,
            project_id: 'other',
          }),
      ),
      http.post('http://agents.test/v1/agents/llm-connections/openai/revoke', () =>
        HttpResponse.json({
          ...REVOKED_OPENAI_SUMMARY,
          provider: 'anthropic',
        }),
      ),
    )

    await expect(listLlmConnections(connection, 'demo')).rejects.toThrow(
      'LLM connection list crossed project authority',
    )
    await expect(
      putLlmConnection(connection, 'openai', 'demo', 'sk-secret', 0),
    ).rejects.toThrow('LLM connection response crossed project authority')
    await expect(getLlmModels(connection, 'openai', 'demo')).rejects.toThrow(
      'LLM model inventory crossed project authority',
    )
    await expect(refreshLlmModels(connection, 'openai', 'demo', 3)).rejects.toThrow(
      'LLM model refresh crossed project authority',
    )
    await expect(
      revokeLlmConnection(connection, 'openai', 'demo', 3, 'Credential rotated'),
    ).rejects.toThrow('LLM connection revocation crossed project authority')
  })
})
