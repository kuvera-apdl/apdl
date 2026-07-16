// Verification engine (plan §5.7): canonical exact-one transport and polling.
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'

import {
  runVerification,
  type StepId,
  type StepState,
} from '../../src/features/verify/verification'
import { makeWorkspace } from '../helpers/fixtures'

let healthOk = true
let pipelineFindsAtAttempt = 1
let countCalls = 0
let ingestCalls = 0
let lastIngestBody: unknown = null

const okHealth = (service: string) => HttpResponse.json({ status: 'ok', service })

const server = setupServer(
  http.get('*/api/projects/demo/ingestion/health', () =>
    healthOk ? okHealth('ingestion') : HttpResponse.json({ status: 'degraded', service: 'ingestion' }, { status: 503 }),
  ),
  http.get('*/api/projects/demo/config/health', () =>
    HttpResponse.json({ status: 'ok', service: 'apdl-config', postgres: 'ok', redis: 'ok', sse_connections: 1 }),
  ),
  http.get('*/api/projects/demo/query/health', () => okHealth('apdl-query')),
  http.get('*/api/projects/demo/query/ready', () => HttpResponse.json({ status: 'ready' })),
  http.get('*/api/projects/demo/agents/health', () => okHealth('apdl-agents')),
  http.get('*/api/projects/demo/agents/ready', () => HttpResponse.json({ status: 'ready' })),
  http.post('*/api/projects/demo/ingestion/v1/events', async ({ request }) => {
    ingestCalls += 1
    lastIngestBody = await request.json()
    return HttpResponse.json({ accepted: 1 }, { status: 202 })
  }),
  http.post('*/api/projects/demo/query/v1/query/events/count', async ({ request }) => {
    countCalls += 1
    const found = countCalls >= pipelineFindsAtAttempt
    const body = (await request.json()) as {
      selectors: { filters: { value: string }[] }[]
    }
    const verificationId = body.selectors[0].filters[0].value
    return HttpResponse.json({
      results: found
        ? [
            {
              selector: `apdl_console_verification[verification_id eq ${verificationId}]`,
              event_name: 'apdl_console_verification',
              event_count: 1,
              unique_users: 1,
            },
          ]
        : [],
      total_events: found ? 1 : 0,
      total_users: found ? 1 : 0,
    })
  }),
  http.get('*/api/projects/demo/config/v1/flags', () =>
    HttpResponse.json(
      { schema_version: 2, project_id: 'demo', flags: [] },
      { headers: { 'X-Cache': 'HIT' } },
    ),
  ),
)

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

beforeEach(() => {
  healthOk = true
  pipelineFindsAtAttempt = 1
  countCalls = 0
  ingestCalls = 0
  lastIngestBody = null
})

function harness() {
  const states = new Map<StepId, Partial<StepState>>()
  return {
    states,
    options: {
      workspace: makeWorkspace(),
      projectId: 'demo',
      getLive: () => ({ status: 'open' as const, lastEventAt: Date.now(), hasServedFlags: true }),
      update: (id: StepId, patch: Partial<StepState>) =>
        states.set(id, { ...states.get(id), ...patch }),
      sleep: () => Promise.resolve(),
      pollIntervalMs: 0,
    },
  }
}

describe('runVerification', () => {
  test('passes end-to-end when every layer answers', async () => {
    const { states, options } = harness()
    const passed = await runVerification(options)
    expect(passed).toBe(true)
    expect(states.get('health')?.status).toBe('ok')
    expect(states.get('ingest')?.status).toBe('ok')
    expect(states.get('pipeline')?.status).toBe('ok')
    expect(states.get('flags')?.status).toBe('ok')
    expect(states.get('flags')?.detail).toContain('X-Cache')
    expect(states.get('sse')?.status).toBe('ok')
    expect(ingestCalls).toBe(1)
    expect(lastIngestBody).toEqual({
      events: [
        expect.objectContaining({
          event: 'apdl_console_verification',
          type: 'track',
          user_id: 'apdl-console-verifier',
          context: { library: { name: 'apdl-admin', version: '1' } },
          message_id: expect.stringMatching(/^admin_verification_[0-9a-f-]+$/),
        }),
      ],
    })
  })

  test('polls a slow pipeline without sending the event more than once', async () => {
    pipelineFindsAtAttempt = 7
    const { states, options } = harness()
    const passed = await runVerification(options)
    expect(passed).toBe(true)
    expect(states.get('pipeline')?.status).toBe('ok')
    expect(ingestCalls).toBe(1)
  })

  test('fails if Query observes a duplicate verification event', async () => {
    server.use(
      http.post('*/api/projects/demo/query/v1/query/events/count', () =>
        HttpResponse.json({
          results: [
            {
              selector: 'apdl_console_verification',
              event_name: 'apdl_console_verification',
              event_count: 2,
              unique_users: 1,
            },
          ],
          total_events: 2,
          total_users: 1,
        }),
      ),
    )
    const { states, options } = harness()
    const passed = await runVerification(options)
    expect(passed).toBe(false)
    expect(states.get('pipeline')?.detail).toContain('exactly one')
    expect(ingestCalls).toBe(1)
  })

  test('fails fast with a writer hint when the pipeline never answers', async () => {
    pipelineFindsAtAttempt = Number.MAX_SAFE_INTEGER
    const { states, options } = harness()
    const passed = await runVerification({ ...options, pollAttempts: 6 })
    expect(passed).toBe(false)
    expect(states.get('pipeline')?.status).toBe('fail')
    expect(states.get('pipeline')?.hint).toContain('clickhouse-writer')
    expect(states.get('flags')?.status ?? 'idle').not.toBe('ok')
  })

  test('stops at step 1 when a service is degraded', async () => {
    healthOk = false
    const { states, options } = harness()
    const passed = await runVerification(options)
    expect(passed).toBe(false)
    expect(states.get('health')?.status).toBe('fail')
    expect(states.get('health')?.detail).toContain('ingestion')
    expect(ingestCalls).toBe(0)
  })
})
