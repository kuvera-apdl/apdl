import { z } from 'zod'

import { adminRoleSchema, authIdentitySchema, type AdminRole, type AuthIdentity } from '@/api/auth'
import { request } from '@/api/http'

const adminConnection = { baseUrl: '', actor: '' }
const projectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)
const uuidSchema = z.string().uuid()

export const HUMAN_ROLE_ORDER = [
  'events:write',
  'config:read',
  'config:write',
  'config:evaluate',
  'query:read',
  'agents:read',
  'agents:run',
  'agents:manage',
  'agents:approve',
  'credentials:manage',
  'members:manage',
] as const satisfies readonly AdminRole[]

export const humanRoleListSchema = z
  .array(adminRoleSchema)
  .min(1)
  .max(HUMAN_ROLE_ORDER.length)
  .superRefine((roles, context) => {
    const selected = new Set(roles)
    const expected = HUMAN_ROLE_ORDER.filter((role) => selected.has(role))
    if (roles.length !== expected.length || roles.some((role, index) => role !== expected[index])) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'roles must be unique and use canonical order',
      })
    }
  })

const creatorSchema = z
  .object({
    user_id: uuidSchema,
    email: z.string().email(),
  })
  .strict()

const ownershipSchema = z.discriminatedUnion('kind', [
  z
    .object({
      kind: z.literal('human'),
      owner_user_id: uuidSchema,
      owner_email: z.string().email(),
    })
    .strict(),
  z.object({ kind: z.literal('operator_managed') }).strict(),
])

const executionAuthorizationSchema = z
  .object({
    authorized: z.boolean(),
    source: z.enum(['operator_provisioned', 'self_registered_override']).nullable(),
  })
  .strict()
  .superRefine((authorization, context) => {
    if (authorization.authorized !== (authorization.source !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'source must be present exactly when execution is authorized',
      })
    }
  })

export const projectAuthorizationSchema = z
  .object({
    project_id: projectIdSchema,
    creator: creatorSchema.nullable(),
    ownership: ownershipSchema,
    execution_authorization: executionAuthorizationSchema,
  })
  .strict()

export const projectMemberSchema = z
  .object({
    user_id: uuidSchema,
    email: z.string().email(),
    roles: humanRoleListSchema,
    active: z.boolean(),
    is_owner: z.boolean(),
    joined_at: z.string().datetime({ offset: true }),
  })
  .strict()

const invitationBlockedReasonSchema = z.enum([
  'inviter_inactive',
  'inviter_not_project_member',
  'inviter_lacks_members_manage',
  'roles_exceed_inviter_authority',
  'members_manage_requires_owner',
])

const pendingInvitationFields = {
  invitation_id: uuidSchema,
  email: z.string().email(),
  roles: humanRoleListSchema,
  inviter_email: z.string().email(),
  status: z.enum(['valid', 'blocked']),
  blocked_reason: invitationBlockedReasonSchema.nullable(),
  expires_at: z.string().datetime({ offset: true }),
  created_at: z.string().datetime({ offset: true }),
}

function validateInvitationStatus(
  invitation: { status: 'valid' | 'blocked'; blocked_reason: string | null },
  context: z.RefinementCtx,
) {
  if ((invitation.status === 'valid') !== (invitation.blocked_reason === null)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['blocked_reason'],
      message: 'blocked_reason must be null exactly when status is valid',
    })
  }
}

export const pendingInvitationSchema = z
  .object(pendingInvitationFields)
  .strict()
  .superRefine(validateInvitationStatus)

export const projectInvitationRevealSchema = z
  .object({
    ...pendingInvitationFields,
    invitation_url: z.string().url(),
  })
  .strict()
  .superRefine(validateInvitationStatus)

export const projectMembersSchema = z
  .object({
    members: z.array(projectMemberSchema),
    pending_invitations: z.array(pendingInvitationSchema),
  })
  .strict()

export const invitationInspectionSchema = z
  .object({
    status: z.literal('valid'),
    project_id: projectIdSchema,
    email: z.string().email(),
    roles: humanRoleListSchema,
    expires_at: z.string().datetime({ offset: true }),
  })
  .strict()

export const membershipAuditEntrySchema = z
  .object({
    audit_id: uuidSchema,
    project_id: projectIdSchema,
    action: z.enum([
      'invitation_create',
      'invitation_revoke',
      'invitation_accept',
      'roles_replace',
      'activation_grant',
      'member_remove',
    ]),
    actor_user_id: uuidSchema,
    actor_email: z.string().email(),
    subject_user_id: uuidSchema.nullable(),
    subject_email: z.string().email(),
    invitation_id: uuidSchema.nullable(),
    previous_roles: humanRoleListSchema.nullable(),
    new_roles: humanRoleListSchema.nullable(),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict()

export const ownershipAuditEntrySchema = z
  .object({
    audit_id: uuidSchema,
    project_id: projectIdSchema,
    previous_owner_user_id: uuidSchema.nullable(),
    previous_owner_email: z.string().email().nullable(),
    new_owner_user_id: uuidSchema,
    new_owner_email: z.string().email(),
    actor: z.string().min(1).max(512),
    reason: z.string().min(1).max(2000),
    created_at: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((entry, context) => {
    if ((entry.previous_owner_user_id === null) !== (entry.previous_owner_email === null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'previous owner ID and email must be present together',
      })
    }
  })

export const auditCursorSchema = z
  .object({
    created_at: z.string().datetime({ offset: true }),
    audit_id: uuidSchema,
  })
  .strict()

export const membershipAuditPageSchema = z
  .object({
    entries: z.array(membershipAuditEntrySchema),
    next_cursor: auditCursorSchema.nullable(),
  })
  .strict()

export const ownershipAuditPageSchema = z
  .object({
    entries: z.array(ownershipAuditEntrySchema),
    next_cursor: auditCursorSchema.nullable(),
  })
  .strict()

export const invitationCreateRequestSchema = z
  .object({
    email: z.string().trim().toLowerCase().email(),
    roles: humanRoleListSchema,
  })
  .strict()

export const memberRolesReplaceRequestSchema = z
  .object({ roles: humanRoleListSchema })
  .strict()

export const ownershipTransferRequestSchema = z
  .object({
    target_user_id: uuidSchema,
    reason: z.string().trim().min(1).max(2000).regex(/^[^\r\n]+$/).optional(),
  })
  .strict()

export type ProjectAuthorization = z.infer<typeof projectAuthorizationSchema>
export type ProjectMember = z.infer<typeof projectMemberSchema>
export type PendingInvitation = z.infer<typeof pendingInvitationSchema>
export type ProjectInvitationReveal = z.infer<typeof projectInvitationRevealSchema>
export type ProjectMembers = z.infer<typeof projectMembersSchema>
export type InvitationInspection = z.infer<typeof invitationInspectionSchema>
export type MembershipAuditEntry = z.infer<typeof membershipAuditEntrySchema>
export type OwnershipAuditEntry = z.infer<typeof ownershipAuditEntrySchema>
export type AuditCursor = z.infer<typeof auditCursorSchema>
export type MembershipAuditPage = z.infer<typeof membershipAuditPageSchema>
export type OwnershipAuditPage = z.infer<typeof ownershipAuditPageSchema>

function projectPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}`
}

function auditPath(path: string, cursor: AuditCursor | null): string {
  const params = new URLSearchParams({ limit: '50' })
  if (cursor) {
    params.set('before_created_at', cursor.created_at)
    params.set('before_audit_id', cursor.audit_id)
  }
  return `${path}?${params.toString()}`
}

export function getProjectAuthorization(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectAuthorization> {
  return request(adminConnection, `${projectPath(projectId)}/authorization`, {
    signal,
    schema: projectAuthorizationSchema,
  })
}

export function listProjectMembers(
  projectId: string,
  signal?: AbortSignal,
): Promise<ProjectMembers> {
  return request(adminConnection, `${projectPath(projectId)}/members`, {
    signal,
    schema: projectMembersSchema,
  })
}

export function createProjectInvitation(
  projectId: string,
  email: string,
  roles: AdminRole[],
): Promise<ProjectInvitationReveal> {
  const body = invitationCreateRequestSchema.parse({ email, roles })
  return request(adminConnection, `${projectPath(projectId)}/invitations`, {
    method: 'POST',
    body,
    schema: projectInvitationRevealSchema,
  })
}

export function revokeProjectInvitation(
  projectId: string,
  invitationId: string,
): Promise<unknown> {
  return request(
    adminConnection,
    `${projectPath(projectId)}/invitations/${encodeURIComponent(invitationId)}`,
    { method: 'DELETE' },
  )
}

export function replaceProjectMemberRoles(
  projectId: string,
  memberUserId: string,
  roles: AdminRole[],
): Promise<ProjectMember> {
  const body = memberRolesReplaceRequestSchema.parse({ roles })
  return request(
    adminConnection,
    `${projectPath(projectId)}/members/${encodeURIComponent(memberUserId)}/roles`,
    { method: 'PUT', body, schema: projectMemberSchema },
  )
}

export function removeProjectMember(projectId: string, memberUserId: string): Promise<unknown> {
  return request(
    adminConnection,
    `${projectPath(projectId)}/members/${encodeURIComponent(memberUserId)}`,
    { method: 'DELETE' },
  )
}

export function transferProjectOwnership(
  projectId: string,
  targetUserId: string,
  reason?: string,
): Promise<ProjectAuthorization> {
  const body = ownershipTransferRequestSchema.parse({
    target_user_id: targetUserId,
    ...(reason === undefined ? {} : { reason }),
  })
  return request(adminConnection, `${projectPath(projectId)}/ownership/transfer`, {
    method: 'POST',
    body,
    schema: projectAuthorizationSchema,
  })
}

export function listMembershipAudit(
  projectId: string,
  cursor: AuditCursor | null = null,
  signal?: AbortSignal,
): Promise<MembershipAuditPage> {
  return request(adminConnection, auditPath(`${projectPath(projectId)}/members/audit`, cursor), {
    signal,
    schema: membershipAuditPageSchema,
  })
}

export function listOwnershipAudit(
  projectId: string,
  cursor: AuditCursor | null = null,
  signal?: AbortSignal,
): Promise<OwnershipAuditPage> {
  return request(adminConnection, auditPath(`${projectPath(projectId)}/ownership/audit`, cursor), {
    signal,
    schema: ownershipAuditPageSchema,
  })
}

export function inspectProjectInvitation(
  rawToken: string,
  signal?: AbortSignal,
): Promise<InvitationInspection> {
  return request(adminConnection, `/api/invitations/${encodeURIComponent(rawToken)}`, {
    signal,
    schema: invitationInspectionSchema,
    redirectOnUnauthorized: false,
  })
}

export function acceptProjectInvitation(rawToken: string): Promise<AuthIdentity> {
  return request(adminConnection, `/api/invitations/${encodeURIComponent(rawToken)}/accept`, {
    method: 'POST',
    schema: authIdentitySchema,
    redirectOnUnauthorized: false,
  })
}

export function registerWithProjectInvitation(
  rawToken: string,
  password: string,
): Promise<AuthIdentity> {
  return request(adminConnection, `/api/invitations/${encodeURIComponent(rawToken)}/register`, {
    method: 'POST',
    body: { password },
    schema: authIdentitySchema,
    redirectOnUnauthorized: false,
  })
}
