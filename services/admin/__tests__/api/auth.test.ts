import { describe, expect, test } from 'vitest'

import { authCapabilitiesSchema } from '../../src/api/auth'

describe('authCapabilitiesSchema', () => {
  test('accepts only the canonical registration capability', () => {
    expect(
      authCapabilitiesSchema.safeParse({ registration_enabled: true }).success,
    ).toBe(true)
    expect(authCapabilitiesSchema.safeParse({}).success).toBe(false)
    expect(
      authCapabilitiesSchema.safeParse({ registration_enabled: 'true' }).success,
    ).toBe(false)
    expect(
      authCapabilitiesSchema.safeParse({
        registration_enabled: true,
        registration_mode: 'open',
      }).success,
    ).toBe(false)
  })
})
