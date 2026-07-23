import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, test } from 'vitest'

import { getRuntimeEvidenceObservations } from '@/api/codegen'
import {
  runtimeEvidenceObservationListSchema,
  runtimeEvidenceObservationSchema,
} from '@/api/schemas/codegen-runtime'
import { makeRuntimeEvidenceObservation } from '../helpers/fixtures'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('codegen runtime evidence', () => {
  test('queries the read-only runtime observations endpoint with a bounded limit', async () => {
    server.use(
      http.get(
        'http://codegen.test/v1/changesets/cs_abc123/runtime-observations',
        ({ request }) => {
          expect(new URL(request.url).searchParams.get('limit')).toBe('25')
          return HttpResponse.json([makeRuntimeEvidenceObservation()])
        },
      ),
    )

    const result = await getRuntimeEvidenceObservations(
      { baseUrl: 'http://codegen.test', actor: 'tester' },
      'cs_abc123',
      { limit: 25 },
    )

    expect(result).toHaveLength(1)
    expect(result[0].assessment.external_ci_status).toBe('pending')
    expect(result[0].artifacts[0].status).toBe('observed')
  })

  test('rejects unknown fields at every runtime observation boundary', () => {
    const fixture = makeRuntimeEvidenceObservation()
    expect(runtimeEvidenceObservationListSchema.safeParse([
      { ...fixture, declares_ci_passed: true },
    ]).success).toBe(false)
    expect(runtimeEvidenceObservationSchema.safeParse({
      ...fixture,
      assessment: { ...fixture.assessment, artifact_success_is_ci_success: true },
    }).success).toBe(false)
  })

  test('rejects stale-head artifacts and assessment evidence', () => {
    const fixture = makeRuntimeEvidenceObservation()
    const bad = {
      ...fixture,
      artifacts: [{ ...fixture.artifacts[0], head_sha: 'f'.repeat(40) }],
    }

    expect(runtimeEvidenceObservationSchema.safeParse(bad).success).toBe(false)
  })

  test('rejects observed requirement evidence without an observed matching artifact', () => {
    const fixture = makeRuntimeEvidenceObservation()
    const bad = {
      ...fixture,
      artifacts: [{ ...fixture.artifacts[0], status: 'unverified', files: [], unverified_reason: 'missing' }],
    }

    expect(runtimeEvidenceObservationSchema.safeParse(bad).success).toBe(false)
  })

  test('rejects unsafe artifact and job links before rendering them', () => {
    const fixture = makeRuntimeEvidenceObservation()
    expect(runtimeEvidenceObservationSchema.safeParse({
      ...fixture,
      artifacts: [{ ...fixture.artifacts[0], github_url: 'http://github.example/artifact' }],
    }).success).toBe(false)
    expect(runtimeEvidenceObservationSchema.safeParse({
      ...fixture,
      job_logs: [{
        ...fixture.job_logs[0],
        github_url: 'https://user:password@github.example/job',
      }],
    }).success).toBe(false)
  })
})
