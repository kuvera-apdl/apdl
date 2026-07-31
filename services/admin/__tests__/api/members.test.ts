import { describe, expect, test } from 'vitest'

import {
  invitationCreateRequestSchema,
  invitationInspectionSchema,
  membershipAuditEntrySchema,
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
  expires_at: '2026-08-06T12:00:00Z',
  created_at: '2026-07-30T12:00:00Z',
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
      membershipAuditEntrySchema.safeParse({
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
  })
})
