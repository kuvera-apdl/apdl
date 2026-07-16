import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, test } from 'vitest'
import { z } from 'zod'

import { ApiError, buildUrl, normalizeBaseUrl, request } from '../../src/api/http'
import { AUTH_UNAUTHORIZED_EVENT } from '../../src/core/auth-events'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

const conn = {
  baseUrl: 'http://config.test',
  actor: 'tester',
}

beforeEach(() => {
  document.cookie = 'apdl_admin_csrf=test-csrf; Path=/'
})

describe('buildUrl', () => {
  test('normalizes trailing slashes and skips undefined params', () => {
    expect(normalizeBaseUrl('http://x/')).toBe('http://x')
    expect(buildUrl('http://x/', '/v1/flags', { a: 1, b: undefined, c: false })).toBe(
      'http://x/v1/flags?a=1&c=false',
    )
  })
})

describe('request', () => {
  test('uses the browser session and CSRF token without exposing a service key', async () => {
    const seen: Record<string, string | null> = {}
    server.use(
      http.get('http://config.test/v1/get', ({ request: req }) => {
        seen.getKey = req.headers.get('x-api-key')
        return HttpResponse.json({})
      }),
      http.post('http://config.test/v1/post', ({ request: req }) => {
        seen.postActor = req.headers.get('x-apdl-actor')
        seen.postContentType = req.headers.get('content-type')
        seen.postCsrf = req.headers.get('x-csrf-token')
        seen.credentials = req.credentials
        return HttpResponse.json({})
      }),
    )
    await request(conn, '/v1/get')
    await request(conn, '/v1/post', { method: 'POST', body: { a: 1 } })
    expect(seen.getKey).toBeNull()
    expect(seen.postActor).toBeNull()
    expect(seen.postContentType).toContain('application/json')
    expect(seen.postCsrf).toBe('test-csrf')
    expect(seen.credentials).toBe('same-origin')
  })

  test('normalizes the {error, message} envelope into ApiError', async () => {
    server.use(
      http.put('http://config.test/v1/admin/flags/x', () =>
        HttpResponse.json(
          { error: 'version_conflict', message: "Flag 'x' is at version 7", current_version: 7 },
          { status: 409 },
        ),
      ),
    )
    const error = await request(conn, '/v1/admin/flags/x', { method: 'PUT', body: {} }).catch(
      (caught: unknown) => caught,
    )
    expect(error).toBeInstanceOf(ApiError)
    const apiError = error as ApiError
    expect(apiError.status).toBe(409)
    expect(apiError.code).toBe('version_conflict')
    expect(apiError.message).toContain('version 7')
    expect((apiError.body as { current_version: number }).current_version).toBe(7)
  })

  test('notifies the auth boundary on 401 unless explicitly suppressed', async () => {
    let unauthorizedEvents = 0
    const onUnauthorized = () => {
      unauthorizedEvents += 1
    }
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized)
    server.use(
      http.get('http://config.test/v1/protected', () =>
        HttpResponse.json({ detail: 'Valid API key required' }, { status: 401 }),
      ),
    )

    await expect(request(conn, '/v1/protected')).rejects.toMatchObject({ status: 401 })
    await expect(
      request(conn, '/v1/protected', { redirectOnUnauthorized: false }),
    ).rejects.toMatchObject({ status: 401 })

    window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized)
    expect(unauthorizedEvents).toBe(1)
  })

  test('maps FastAPI 422 validation detail to a field-path message', async () => {
    server.use(
      http.post('http://config.test/v1/admin/flags', () =>
        HttpResponse.json(
          {
            detail: [
              { loc: ['body', 'variants'], msg: 'variants must contain unique keys', type: 'value_error' },
            ],
          },
          { status: 422 },
        ),
      ),
    )
    const error = (await request(conn, '/v1/admin/flags', { method: 'POST', body: {} }).catch(
      (caught: unknown) => caught,
    )) as ApiError
    expect(error.code).toBe('validation_error')
    expect(error.message).toBe('variants: variants must contain unique keys')
  })

  test('retries GETs on 5xx, never mutations', async () => {
    let getAttempts = 0
    let postAttempts = 0
    server.use(
      http.get('http://config.test/v1/flaky', () => {
        getAttempts += 1
        if (getAttempts === 1) return HttpResponse.json({}, { status: 500 })
        return HttpResponse.json({ ok: true })
      }),
      http.post('http://config.test/v1/flaky', () => {
        postAttempts += 1
        return HttpResponse.json({}, { status: 500 })
      }),
    )
    await expect(request(conn, '/v1/flaky')).resolves.toEqual({ ok: true })
    expect(getAttempts).toBe(2)
    await expect(request(conn, '/v1/flaky', { method: 'POST', body: {} })).rejects.toMatchObject({
      status: 500,
    })
    expect(postAttempts).toBe(1)
  })

  test('does not retry semantic errors (409)', async () => {
    let attempts = 0
    server.use(
      http.get('http://config.test/v1/conflict', () => {
        attempts += 1
        return HttpResponse.json({ error: 'conflict', message: 'nope' }, { status: 409 })
      }),
    )
    await expect(request(conn, '/v1/conflict')).rejects.toMatchObject({ code: 'conflict' })
    expect(attempts).toBe(1)
  })

  test('throws schema_mismatch when the response drifts from the canonical mirror', async () => {
    server.use(
      http.get('http://config.test/v1/drift', () => HttpResponse.json({ count: 'three' })),
    )
    const schema = z.object({ count: z.number() }).strict()
    const error = (await request(conn, '/v1/drift', { schema }).catch(
      (caught: unknown) => caught,
    )) as ApiError
    expect(error.code).toBe('schema_mismatch')
    expect(error.message).toContain('count')
  })
})
