import { describe, expect, test } from 'vitest'

import { createFlagExampleCurl } from '../../src/api/config'

const conn = {
  baseUrl: 'http://config.test',
  actor: 'tester',
}

describe('Config API examples', () => {
  test('creates an untargeted flag with full initial rollout', () => {
    const example = createFlagExampleCurl(conn)

    expect(example.body).toMatchObject({
      fallthrough: {
        rollout: { percentage: 100, bucket_by: 'user_id' },
      },
    })
    expect(example.body).not.toHaveProperty('initial_rollout')
  })
})
