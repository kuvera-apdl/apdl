import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, describe, expect, test } from 'vitest'

import { getChangesetObservations } from '@/api/codegen'
import { changesetObservationHistorySchema } from '@/api/schemas/codegen-observations'
import { makeChangesetObservationHistory } from '../helpers/fixtures'

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

describe('codegen observation history', () => {
  test('parses strict PR, exact-head CI, and immutable remediation events', () => {
    const parsed = changesetObservationHistorySchema.parse(makeChangesetObservationHistory())

    expect(parsed.pull_requests[0].head_sha).toBe('c'.repeat(40))
    expect(parsed.ci_verifications[0].signals[0].signal_id).toBe('check_run:101')
    expect(parsed.remediation_attempts[0].event_id).toBe('repair-1:1')
    expect(parsed.remediation_attempts[0].prompt_evidence[0].stage).toBe('repair')
  })

  test('queries the read-only observations endpoint with the canonical schema', async () => {
    server.use(
      http.get('http://codegen.test/v1/changesets/cs_abc123/observations', () =>
        HttpResponse.json(makeChangesetObservationHistory()),
      ),
    )

    const result = await getChangesetObservations(
      { baseUrl: 'http://codegen.test', actor: 'tester' },
      'cs_abc123',
    )

    expect(result.schema_version).toBe('changeset_observation_history@1')
    expect(result.pull_requests).toHaveLength(1)
  })

  test('rejects CI passed without a successful observed signal', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      ci_verifications: [
        {
          ...fixture.ci_verifications[0],
          status: 'passed',
          signals: [],
          requirement_results: [],
          failure_key: null,
          failure_summary: null,
        },
      ],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test('rejects requirement verdicts that contradict their matched signal', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      ci_verifications: [
        {
          ...fixture.ci_verifications[0],
          requirement_results: [
            {
              ...fixture.ci_verifications[0].requirement_results[0],
              status: 'passed',
            },
          ],
        },
      ],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test('rejects a PR action that conflicts with GitHub status', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      pull_requests: [
        {
          ...fixture.pull_requests[0],
          action: 'opened',
          status: 'closed',
        },
      ],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test.each([
    'javascript:alert(1)',
    'http://github.example/acme/widgets/pull/17',
    'https://operator:secret@github.example/acme/widgets/pull/17',
    'https://github.example/acme/widgets/pull/17#https://attacker.example',
  ])('rejects an unsafe rendered observation URL: %s', (githubUrl) => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      pull_requests: [{ ...fixture.pull_requests[0], github_url: githubUrl }],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test('rejects mutable-looking remediation identity drift', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      remediation_attempts: [
        {
          ...fixture.remediation_attempts[0],
          event_id: 'repair-1:2',
        },
      ],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test('requires runtime evidence provenance as an observation ID and hash pair', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      remediation_attempts: [
        {
          ...fixture.remediation_attempts[0],
          runtime_evidence_observation_id: `runtime_obs_${'a'.repeat(32)}`,
        },
      ],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })

  test('rejects unknown nested observation fields', () => {
    const fixture = makeChangesetObservationHistory()
    const bad = {
      ...fixture,
      pull_requests: [{ ...fixture.pull_requests[0], mutable: true }],
    }

    expect(changesetObservationHistorySchema.safeParse(bad).success).toBe(false)
  })
})
