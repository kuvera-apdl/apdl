import { z } from 'zod'

import { request } from '@/api/http'

export const credentialKindSchema = z.enum(['browser', 'confidential'])
export const credentialRoleSchema = z.enum([
  'events:write',
  'config:read',
  'config:evaluate',
  'query:read',
])

export const CREDENTIAL_ROLE_ORDER = [
  'events:write',
  'config:read',
  'config:evaluate',
  'query:read',
] as const
export const BROWSER_CREDENTIAL_ROLES = ['events:write', 'config:read'] as const

export type CredentialKind = z.infer<typeof credentialKindSchema>
export type CredentialRole = z.infer<typeof credentialRoleSchema>

function isCanonicalRoleList(roles: readonly CredentialRole[]): boolean {
  const expected = CREDENTIAL_ROLE_ORDER.filter((role) => roles.includes(role))
  return expected.length === roles.length && expected.every((role, index) => role === roles[index])
}

const canonicalCredentialRolesSchema = z
  .array(credentialRoleSchema)
  .min(1)
  .superRefine((roles, context) => {
    if (new Set(roles).size !== roles.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'roles must not contain duplicates',
      })
    }
    if (!isCanonicalRoleList(roles)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'roles must use canonical order',
      })
    }
  })

const browserCredentialRolesSchema = z.tuple([
  z.literal(BROWSER_CREDENTIAL_ROLES[0]),
  z.literal(BROWSER_CREDENTIAL_ROLES[1]),
])

export const credentialCreateRequestSchema = z.discriminatedUnion('credential_kind', [
  z
    .object({
      credential_kind: z.literal('browser'),
      roles: browserCredentialRolesSchema,
    })
    .strict(),
  z
    .object({
      credential_kind: z.literal('confidential'),
      roles: canonicalCredentialRolesSchema,
    })
    .strict(),
])

const credentialIdSchema = z.string().regex(/^managed-[0-9a-f]{32}$/)
const projectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)
const timestampSchema = z.string().datetime({ offset: true })

const credentialMetadataObjectSchema = z
  .object({
    credential_id: credentialIdSchema,
    project_id: projectIdSchema,
    credential_kind: credentialKindSchema,
    key_prefix: z.string().min(1).max(72),
    roles: canonicalCredentialRolesSchema,
    active: z.boolean(),
    created_at: timestampSchema,
    revoked_at: timestampSchema.nullable(),
    rotated_from_credential_id: credentialIdSchema.nullable(),
  })
  .strict()

function validateCredentialMetadata(
  credential: z.infer<typeof credentialMetadataObjectSchema>,
  context: z.RefinementCtx,
): void {
  const expectedPrefix =
    credential.credential_kind === 'browser'
      ? `client_${credential.project_id}_`
      : `proj_${credential.project_id}_`
  if (credential.key_prefix !== expectedPrefix) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['key_prefix'],
      message: `key_prefix must equal ${expectedPrefix}`,
    })
  }
  if (
    credential.credential_kind === 'browser' &&
    (credential.roles.length !== BROWSER_CREDENTIAL_ROLES.length ||
      !BROWSER_CREDENTIAL_ROLES.every((role, index) => credential.roles[index] === role))
  ) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['roles'],
      message: 'browser credentials require the exact browser role set',
    })
  }
  if (credential.active && credential.revoked_at !== null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['revoked_at'],
      message: 'active credentials cannot have revoked_at',
    })
  }
  if (!credential.active && credential.revoked_at === null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['revoked_at'],
      message: 'revoked credentials require revoked_at',
    })
  }
}

export const credentialMetadataSchema =
  credentialMetadataObjectSchema.superRefine(validateCredentialMetadata)

export const credentialListSchema = z.array(credentialMetadataSchema)

export const credentialRevealSchema = credentialMetadataObjectSchema
  .extend({
    api_key: z.string().min(32).max(256),
  })
  .strict()
  .superRefine((credential, context) => {
    validateCredentialMetadata(credential, context)
    if (!credential.api_key.startsWith(credential.key_prefix)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['api_key'],
        message: 'api_key must use the declared key_prefix',
      })
    }
    const secret = credential.api_key.slice(credential.key_prefix.length)
    if (!/^[A-Za-z0-9]{16,128}$/.test(secret)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['api_key'],
        message: 'api_key secret must contain 16-128 alphanumeric characters',
      })
    }
  })

export const credentialAuditActionSchema = z.enum(['create', 'rotate', 'revoke'])

export const credentialAuditEntrySchema = z
  .object({
    audit_id: z.string().uuid(),
    credential_id: credentialIdSchema,
    project_id: projectIdSchema,
    action: credentialAuditActionSchema,
    actor_user_id: z.string().uuid(),
    actor_email: z.string().email(),
    credential_kind: credentialKindSchema,
    roles: canonicalCredentialRolesSchema,
    successor_credential_id: credentialIdSchema.nullable(),
    created_at: timestampSchema,
  })
  .strict()
  .superRefine((entry, context) => {
    if (
      entry.credential_kind === 'browser' &&
      (entry.roles.length !== BROWSER_CREDENTIAL_ROLES.length ||
        !BROWSER_CREDENTIAL_ROLES.every((role, index) => entry.roles[index] === role))
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['roles'],
        message: 'browser credentials require the exact browser role set',
      })
    }
    if (entry.action === 'rotate' && entry.successor_credential_id === null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['successor_credential_id'],
        message: 'rotate audit entries require a successor',
      })
    }
    if (entry.action !== 'rotate' && entry.successor_credential_id !== null) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['successor_credential_id'],
        message: 'only rotate audit entries may name a successor',
      })
    }
  })

export const credentialAuditListSchema = z.array(credentialAuditEntrySchema)

export type CredentialCreateRequest = z.infer<typeof credentialCreateRequestSchema>
export type CredentialMetadata = z.infer<typeof credentialMetadataSchema>
export type CredentialReveal = z.infer<typeof credentialRevealSchema>
export type CredentialAuditEntry = z.infer<typeof credentialAuditEntrySchema>

const adminConnection = { baseUrl: '', actor: '' }
const emptyBodySchema = z.object({}).strict()

function credentialsPath(projectId: string): string {
  return `/api/projects/${encodeURIComponent(projectId)}/credentials`
}

function credentialPath(projectId: string, credentialId: string): string {
  return `${credentialsPath(projectId)}/${encodeURIComponent(credentialId)}`
}

export function listProjectCredentials(projectId: string): Promise<CredentialMetadata[]> {
  return request(adminConnection, credentialsPath(projectId), {
    schema: credentialListSchema,
  })
}

export function createProjectCredential(
  projectId: string,
  body: CredentialCreateRequest,
): Promise<CredentialReveal> {
  return request(adminConnection, credentialsPath(projectId), {
    method: 'POST',
    body: credentialCreateRequestSchema.parse(body),
    schema: credentialRevealSchema,
  })
}

export function rotateProjectCredential(
  projectId: string,
  credentialId: string,
): Promise<CredentialReveal> {
  return request(adminConnection, `${credentialPath(projectId, credentialId)}/rotate`, {
    method: 'POST',
    body: emptyBodySchema.parse({}),
    schema: credentialRevealSchema,
  })
}

export function revokeProjectCredential(
  projectId: string,
  credentialId: string,
): Promise<CredentialMetadata> {
  return request(adminConnection, `${credentialPath(projectId, credentialId)}/revoke`, {
    method: 'POST',
    body: emptyBodySchema.parse({}),
    schema: credentialMetadataSchema,
  })
}

export function getProjectCredentialAudit(
  projectId: string,
  credentialId: string,
): Promise<CredentialAuditEntry[]> {
  return request(adminConnection, `${credentialPath(projectId, credentialId)}/audit`, {
    schema: credentialAuditListSchema,
  })
}
