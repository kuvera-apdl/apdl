import { describe, expect, test } from 'vitest'

import {
  credentialAuditEntrySchema,
  credentialCreateRequestSchema,
  credentialListSchema,
  credentialMetadataSchema,
  credentialRevealSchema,
} from '../../src/api/credentials'

const BROWSER_ID = `managed-${'1'.repeat(32)}`
const SUCCESSOR_ID = `managed-${'2'.repeat(32)}`

const BROWSER_METADATA = {
  credential_id: BROWSER_ID,
  project_id: 'demo',
  credential_kind: 'browser',
  key_prefix: 'client_demo_',
  roles: ['events:write', 'config:read'],
  active: true,
  created_at: '2026-07-16T12:00:00+00:00',
  revoked_at: null,
  rotated_from_credential_id: null,
} as const

describe('credential schemas', () => {
  test('accepts the canonical metadata, reveal, and audit contracts', () => {
    expect(credentialMetadataSchema.safeParse(BROWSER_METADATA).success).toBe(true)
    expect(
      credentialRevealSchema.safeParse({
        ...BROWSER_METADATA,
        api_key: 'client_demo_1234567890abcdef1234567890abcdef1234567890abcdef',
      }).success,
    ).toBe(true)
    expect(
      credentialAuditEntrySchema.safeParse({
        audit_id: '10000000-0000-4000-8000-000000000001',
        project_id: 'demo',
        credential_id: BROWSER_ID,
        action: 'rotate',
        actor_user_id: '20000000-0000-4000-8000-000000000002',
        actor_email: 'owner@example.com',
        credential_kind: 'browser',
        roles: ['events:write', 'config:read'],
        successor_credential_id: SUCCESSOR_ID,
        created_at: '2026-07-16T12:05:00+00:00',
      }).success,
    ).toBe(true)
  })

  test('keeps secrets and non-canonical fields out of metadata listings', () => {
    expect(
      credentialListSchema.safeParse([
        { ...BROWSER_METADATA, api_key: 'client_demo_should-not-be-listed' },
      ]).success,
    ).toBe(false)
    expect(
      credentialMetadataSchema.safeParse({
        ...BROWSER_METADATA,
        expires_at: null,
      }).success,
    ).toBe(false)
    expect(
      credentialMetadataSchema.safeParse({
        ...BROWSER_METADATA,
        rotated_from: `managed-${'0'.repeat(32)}`,
      }).success,
    ).toBe(false)
  })

  test('enforces exact browser scope and canonical confidential role order', () => {
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'browser',
        roles: ['events:write', 'config:read'],
      }).success,
    ).toBe(true)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'browser',
        roles: ['config:read', 'events:write'],
      }).success,
    ).toBe(false)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'browser',
        roles: ['events:write', 'config:read', 'query:read'],
      }).success,
    ).toBe(false)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'confidential',
        roles: ['events:write', 'config:evaluate', 'query:read'],
      }).success,
    ).toBe(true)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'confidential',
        roles: ['query:read', 'events:write'],
      }).success,
    ).toBe(false)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'confidential',
        roles: ['events:write', 'events:write'],
      }).success,
    ).toBe(false)
    expect(
      credentialCreateRequestSchema.safeParse({
        credential_kind: 'confidential',
        roles: ['config:write'],
      }).success,
    ).toBe(false)
  })

  test('rejects inconsistent prefixes, lifecycle state, and audit vocabulary', () => {
    expect(
      credentialMetadataSchema.safeParse({
        ...BROWSER_METADATA,
        key_prefix: 'proj_demo_',
      }).success,
    ).toBe(false)
    expect(
      credentialMetadataSchema.safeParse({
        ...BROWSER_METADATA,
        revoked_at: '2026-07-16T12:10:00+00:00',
      }).success,
    ).toBe(false)
    expect(
      credentialMetadataSchema.safeParse({
        ...BROWSER_METADATA,
        active: false,
      }).success,
    ).toBe(false)
    expect(
      credentialRevealSchema.safeParse({
        ...BROWSER_METADATA,
        api_key: 'proj_demo_wrong-kind-secret',
      }).success,
    ).toBe(false)
    expect(
      credentialAuditEntrySchema.safeParse({
        audit_id: '10000000-0000-4000-8000-000000000001',
        project_id: 'demo',
        credential_id: BROWSER_ID,
        action: 'rotated',
        actor_user_id: '20000000-0000-4000-8000-000000000002',
        actor_email: 'owner@example.com',
        credential_kind: 'browser',
        roles: ['events:write', 'config:read'],
        successor_credential_id: SUCCESSOR_ID,
        created_at: '2026-07-16T12:05:00+00:00',
      }).success,
    ).toBe(false)
    expect(
      credentialAuditEntrySchema.safeParse({
        audit_id: '10000000-0000-4000-8000-000000000001',
        project_id: 'demo',
        credential_id: BROWSER_ID,
        action: 'rotate',
        actor_user_id: '20000000-0000-4000-8000-000000000002',
        actor_email: 'owner@example.com',
        credential_kind: 'browser',
        roles: ['events:write', 'config:read'],
        successor_credential_id: null,
        created_at: '2026-07-16T12:05:00+00:00',
      }).success,
    ).toBe(false)
  })
})
