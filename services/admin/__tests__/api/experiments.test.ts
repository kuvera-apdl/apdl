import { describe, expect, test } from 'vitest'

import { experimentResultsCurl } from '../../src/api/experiments'

const conn = {
  baseUrl: 'http://query.test',
  actor: 'tester',
}

describe('experiment results project assertion', () => {
  test('accepts only a canonical project ID', () => {
    const spec = experimentResultsCurl(conn, 'experiment-1', { projectId: 'Demo123' })

    expect(spec.url).toBe(
      'http://query.test/v1/query/experiment/experiment-1?project_id=Demo123',
    )
    for (const projectId of ['demo-project', 'demo_project', ' demo', 'A'.repeat(65)]) {
      expect(() => experimentResultsCurl(conn, 'experiment-1', { projectId })).toThrow()
    }
  })
})
