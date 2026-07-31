import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, test, vi } from 'vitest'

import {
  createLlmConnection,
  getLlmConnection,
  listLlmConnections,
  llmConnectionDetailSchema,
  llmConnectionListSchema,
  llmConnectionSummarySchema,
  refreshLlmConnection,
  replaceLlmConnection,
  revokeLlmConnection,
  type LlmConnectionDetail,
  type LlmConnectionSummary,
} from '../../src/api/llmConnections'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const service = { baseUrl: 'http://vault.test', actor: 'tester' }
const CONNECTION_ID = '10000000-0000-4000-8000-000000000001'

const SUMMARY: LlmConnectionSummary = {
  schema_version: 'project_llm_connection@1',
  connection_id: CONNECTION_ID,
  project_id: 'demo',
  provider: 'openai',
  label: 'Production',
  version: 3,
  inventory_version: 4,
  state: 'active',
  consumers: ['agents', 'codegen'],
  validated_at: '2026-08-01T12:00:00Z',
  created_at: '2026-08-01T11:00:00Z',
  updated_at: '2026-08-01T12:00:00Z',
  revoked_at: null,
  model_count: 1,
}

const DETAIL: LlmConnectionDetail = {
  ...SUMMARY,
  models: [
    {
      schema_version: 'project_llm_provider_model@1',
      model_id: 'gpt-5.4-mini',
    },
  ],
}

const REVOKED: LlmConnectionSummary = {
  ...SUMMARY,
  version: 4,
  inventory_version: 5,
  state: 'revoked',
  consumers: [],
  model_count: 0,
  updated_at: '2026-08-01T13:00:00Z',
  revoked_at: '2026-08-01T13:00:00Z',
}

describe('project LLM vault schemas', () => {
  test('accept strict secret-free contracts', () => {
    expect(llmConnectionSummarySchema.safeParse(SUMMARY).success).toBe(true)
    expect(llmConnectionDetailSchema.safeParse(DETAIL).success).toBe(true)
    expect(
      llmConnectionListSchema.safeParse({
        schema_version: 'project_llm_connection_list@1',
        project_id: 'demo',
        connections: [SUMMARY],
      }).success,
    ).toBe(true)
    expect(
      llmConnectionDetailSchema.safeParse({ ...DETAIL, api_key: 'secret' }).success,
    ).toBe(false)
    expect(
      llmConnectionDetailSchema.safeParse({
        ...DETAIL,
        models: [{ ...DETAIL.models[0], endpoint_host: 'api.openai.com' }],
      }).success,
    ).toBe(false)
  })
})

describe('project LLM vault API', () => {
  test('uses exact canonical paths and bodies', async () => {
    const urls: string[] = []
    const bodies: unknown[] = []
    server.use(
      http.get('http://vault.test/v1/llm-connections', ({ request }) => {
        urls.push(request.url)
        return HttpResponse.json({
          schema_version: 'project_llm_connection_list@1',
          project_id: 'demo',
          connections: [SUMMARY],
        })
      }),
      http.post('http://vault.test/v1/llm-connections', async ({ request }) => {
        urls.push(request.url)
        bodies.push(await request.json())
        return HttpResponse.json(DETAIL)
      }),
      http.get(`http://vault.test/v1/llm-connections/${CONNECTION_ID}`, ({ request }) => {
        urls.push(request.url)
        return HttpResponse.json(DETAIL)
      }),
      http.put(`http://vault.test/v1/llm-connections/${CONNECTION_ID}`, async ({ request }) => {
        urls.push(request.url)
        bodies.push(await request.json())
        return HttpResponse.json(DETAIL)
      }),
      http.post(
        `http://vault.test/v1/llm-connections/${CONNECTION_ID}/refresh`,
        async ({ request }) => {
          urls.push(request.url)
          bodies.push(await request.json())
          return HttpResponse.json(DETAIL)
        },
      ),
      http.post(
        `http://vault.test/v1/llm-connections/${CONNECTION_ID}/revoke`,
        async ({ request }) => {
          urls.push(request.url)
          bodies.push(await request.json())
          return HttpResponse.json(REVOKED)
        },
      ),
    )

    await listLlmConnections(service, 'demo')
    await createLlmConnection(
      service,
      'demo',
      'openai',
      'Production',
      'sk-secret',
      ['agents', 'codegen'],
    )
    await getLlmConnection(service, CONNECTION_ID, 'demo')
    await replaceLlmConnection(service, SUMMARY, 'Production', 'sk-new', ['agents'])
    await refreshLlmConnection(service, SUMMARY)
    await revokeLlmConnection(service, SUMMARY, 'Credential retired')

    expect(urls).toEqual([
      'http://vault.test/v1/llm-connections?project_id=demo',
      'http://vault.test/v1/llm-connections',
      `http://vault.test/v1/llm-connections/${CONNECTION_ID}?project_id=demo`,
      `http://vault.test/v1/llm-connections/${CONNECTION_ID}`,
      `http://vault.test/v1/llm-connections/${CONNECTION_ID}/refresh`,
      `http://vault.test/v1/llm-connections/${CONNECTION_ID}/revoke`,
    ])
    expect(bodies).toEqual([
      {
        project_id: 'demo',
        provider: 'openai',
        label: 'Production',
        api_key: 'sk-secret',
        consumers: ['agents', 'codegen'],
      },
      {
        project_id: 'demo',
        provider: 'openai',
        label: 'Production',
        api_key: 'sk-new',
        consumers: ['agents'],
        version: 3,
      },
      { project_id: 'demo', version: 3 },
      { project_id: 'demo', version: 3, reason: 'Credential retired' },
    ])
  })

  test('rejects secret-bearing and cross-project responses', async () => {
    server.use(
      http.get('http://vault.test/v1/llm-connections', () =>
        HttpResponse.json({
          schema_version: 'project_llm_connection_list@1',
          project_id: 'other',
          connections: [],
        }),
      ),
      http.post('http://vault.test/v1/llm-connections', () =>
        HttpResponse.json({ ...DETAIL, api_key: 'leaked' }),
      ),
    )
    await expect(listLlmConnections(service, 'demo')).rejects.toThrow(
      'crossed project authority',
    )
    await expect(
      createLlmConnection(service, 'demo', 'openai', 'Production', 'secret', ['agents']),
    ).rejects.toMatchObject({ code: 'schema_mismatch' })
  })

  test('provider rejection does not emit an expired-session event', async () => {
    const unauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    server.use(
      http.post('http://vault.test/v1/llm-connections', () =>
        HttpResponse.json(
          { detail: { code: 'invalid_key', message: 'Provider rejected the credential' } },
          { status: 401 },
        ),
      ),
    )
    try {
      await expect(
        createLlmConnection(service, 'demo', 'openai', 'Production', 'bad', ['agents']),
      ).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).not.toHaveBeenCalled()
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    }
  })

  test('ordinary unauthorized responses emit an expired-session event', async () => {
    const unauthorized = vi.fn()
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    server.use(
      http.post('http://vault.test/v1/llm-connections', () =>
        HttpResponse.json(
          { detail: 'Authentication required' },
          { status: 401 },
        ),
      ),
    )
    try {
      await expect(
        createLlmConnection(service, 'demo', 'openai', 'Production', 'bad', ['agents']),
      ).rejects.toMatchObject({ status: 401 })
      expect(unauthorized).toHaveBeenCalledOnce()
    } finally {
      window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, unauthorized)
    }
  })
})
