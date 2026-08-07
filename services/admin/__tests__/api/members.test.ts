import { describe, expect, test } from 'vitest'

import {
  invitationCreateRequestSchema,
  invitationInspectionSchema,
  membershipAuditEntrySchema,
  membershipAuditPageSchema,
  ownershipAuditPageSchema,
  ownershipTransferRequestSchema,
  pendingInvitationSchema,
  projectAuthorizationSchema,
  projectInvitationRevealSchema,
  projectMemberSchema,
} from '../../src/api/members'

const MEMBER = {
  user_id: '20000000-0000-4000-8000-000000000002',
  email: 'owner@example.com',
  roles: ['config:read', 'members:manage'],
  active: true,
  is_owner: true,
  joined_at: '2026-07-30T12:00:00Z',
}

const INVITATION = {
  invitation_id: '30000000-0000-4000-8000-000000000003',
  email: 'invitee@example.com',
  roles: ['config:read'],
  inviter_email: 'owner@example.com',
  status: 'valid',
  blocked_reason: null,
  expires_at: '2026-08-06T12:00:00Z',
  created_at: '2026-07-30T12:00:00Z',
}

const MEMBERSHIP_AUDIT_ENTRY = {
  audit_id: '40000000-0000-4000-8000-000000000004',
  project_id: 'demo',
  action: 'roles_replace',
  actor_user_id: MEMBER.user_id,
  actor_email: MEMBER.email,
  subject_user_id: '50000000-0000-4000-8000-000000000005',
  subject_email: 'member@example.com',
  invitation_id: null,
  previous_roles: ['config:read'],
  new_roles: ['config:read', 'config:write'],
  created_at: '2026-07-30T12:00:00Z',
}

const AUDIT_CURSOR = {
  created_at: MEMBERSHIP_AUDIT_ENTRY.created_at,
  audit_id: MEMBERSHIP_AUDIT_ENTRY.audit_id,
}

describe('project membership schemas', () => {
  test('accepts canonical ownership, member, invitation, and audit contracts', () => {
    expect(
      projectAuthorizationSchema.safeParse({
        project_id: 'demo',
        creator: {
          user_id: '10000000-0000-4000-8000-000000000001',
          email: 'creator@example.com',
        },
        ownership: {
          kind: 'human',
          owner_user_id: MEMBER.user_id,
          owner_email: MEMBER.email,
        },
        execution_authorization: {
          authorized: true,
          source: 'operator_provisioned',
        },
      }).success,
    ).toBe(true)
    expect(projectMemberSchema.safeParse(MEMBER).success).toBe(true)
    expect(pendingInvitationSchema.safeParse(INVITATION).success).toBe(true)
    expect(
      pendingInvitationSchema.safeParse({
        ...INVITATION,
        status: 'blocked',
        blocked_reason: 'inviter_inactive',
      }).success,
    ).toBe(true)
    expect(
      projectInvitationRevealSchema.safeParse({
        ...INVITATION,
        invitation_url: `https://admin.example/invitations/${'a'.repeat(43)}`,
      }).success,
    ).toBe(true)
    expect(
      invitationInspectionSchema.safeParse({
        status: 'valid',
        project_id: 'demo',
        email: INVITATION.email,
        roles: INVITATION.roles,
        expires_at: INVITATION.expires_at,
      }).success,
    ).toBe(true)
    expect(
      membershipAuditEntrySchema.safeParse(MEMBERSHIP_AUDIT_ENTRY).success,
    ).toBe(true)
    expect(
      membershipAuditEntrySchema.safeParse({
        ...MEMBERSHIP_AUDIT_ENTRY,
        action: 'activation_grant',
        actor_user_id: MEMBERSHIP_AUDIT_ENTRY.subject_user_id,
      }).success,
    ).toBe(true)
    expect(
      membershipAuditPageSchema.safeParse({
        entries: [MEMBERSHIP_AUDIT_ENTRY],
        next_cursor: AUDIT_CURSOR,
      }).success,
    ).toBe(true)
    expect(
      ownershipAuditPageSchema.safeParse({
        entries: [
          {
            audit_id: '60000000-0000-4000-8000-000000000006',
            project_id: 'demo',
            previous_owner_user_id: MEMBER.user_id,
            previous_owner_email: MEMBER.email,
            new_owner_user_id: '70000000-0000-4000-8000-000000000007',
            new_owner_email: 'new-owner@example.com',
            actor: MEMBER.email,
            reason: 'Planned team handoff',
            created_at: '2026-07-31T12:00:00Z',
          },
        ],
        next_cursor: null,
      }).success,
    ).toBe(true)
  })

  test('rejects aliases, leaked secret material, and noncanonical roles', () => {
    expect(
      pendingInvitationSchema.safeParse({
        ...INVITATION,
        invitation_url: 'https://admin.example/invitations/secret',
      }).success,
    ).toBe(false)
    expect(
      pendingInvitationSchema.safeParse({
        ...INVITATION,
        token_hash: 'a'.repeat(64),
      }).success,
    ).toBe(false)
    expect(
      invitationCreateRequestSchema.safeParse({
        email: 'invitee@example.com',
        roles: ['members:manage', 'config:read'],
      }).success,
    ).toBe(false)
    expect(
      pendingInvitationSchema.safeParse({
        ...INVITATION,
        status: 'valid',
        blocked_reason: 'inviter_inactive',
      }).success,
    ).toBe(false)
    expect(
      pendingInvitationSchema.safeParse({
        ...INVITATION,
        status: 'blocked',
        blocked_reason: null,
      }).success,
    ).toBe(false)
    expect(
      invitationCreateRequestSchema.safeParse({
        email: 'invitee@example.com',
        roles: ['config:read', 'config:read'],
      }).success,
    ).toBe(false)
    expect(
      projectAuthorizationSchema.safeParse({
        project_id: 'demo',
        creator: null,
        ownership: { kind: 'operator_managed', owner_user_id: MEMBER.user_id },
        execution_authorization: { authorized: false, source: 'operator_provisioned' },
      }).success,
    ).toBe(false)
    expect(membershipAuditPageSchema.safeParse([MEMBERSHIP_AUDIT_ENTRY]).success).toBe(false)
    expect(
      membershipAuditEntrySchema.safeParse({
        ...MEMBERSHIP_AUDIT_ENTRY,
        action: 'setup_activation',
      }).success,
    ).toBe(false)
    expect(
      membershipAuditPageSchema.safeParse({
        entries: [MEMBERSHIP_AUDIT_ENTRY],
        next_cursor: { created_at: AUDIT_CURSOR.created_at },
      }).success,
    ).toBe(false)
    expect(
      membershipAuditPageSchema.safeParse({
        entries: [MEMBERSHIP_AUDIT_ENTRY],
        next_cursor: { ...AUDIT_CURSOR, offset: 50 },
      }).success,
    ).toBe(false)
  })

  test('normalizes and strictly validates ownership transfer context', () => {
    expect(
      ownershipTransferRequestSchema.parse({
        target_user_id: MEMBER.user_id,
        reason: '  Planned team handoff  ',
      }),
    ).toEqual({
      target_user_id: MEMBER.user_id,
      reason: 'Planned team handoff',
    })
    expect(
      ownershipTransferRequestSchema.safeParse({
        target_user_id: MEMBER.user_id,
        reason: 'invalid\nreason',
      }).success,
    ).toBe(false)
    expect(
      ownershipTransferRequestSchema.safeParse({
        target_user_id: MEMBER.user_id,
        actor: MEMBER.email,
      }).success,
    ).toBe(false)
  })
})
