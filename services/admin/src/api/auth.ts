import { z } from 'zod'

import { request } from '@/api/http'

export const adminRoleSchema = z.enum([
  'events:write',
  'config:read',
  'config:write',
  'config:evaluate',
  'query:read',
  'agents:read',
  'agents:run',
  'agents:manage',
  'agents:approve',
])

export type AdminRole = z.infer<typeof adminRoleSchema>

export const projectAccessSchema = z
  .object({
    project_id: z.string().regex(/^[A-Za-z0-9]{1,64}$/),
    roles: z.array(adminRoleSchema).min(1),
  })
  .strict()

export const authIdentitySchema = z
  .object({
    user_id: z.string().uuid(),
    email: z.string().email(),
    projects: z.array(projectAccessSchema),
  })
  .strict()

export type AuthIdentity = z.infer<typeof authIdentitySchema>

const authConnection = { baseUrl: '', actor: '' }

export function loginAdmin(email: string, password: string): Promise<AuthIdentity> {
  return request(authConnection, '/api/auth/login', {
    method: 'POST',
    body: { email, password },
    schema: authIdentitySchema,
    redirectOnUnauthorized: false,
  })
}

export function registerAdmin(email: string, password: string): Promise<AuthIdentity> {
  return request(authConnection, '/api/auth/register', {
    method: 'POST',
    body: { email, password },
    schema: authIdentitySchema,
    redirectOnUnauthorized: false,
  })
}

export function createAdminProject(projectId: string): Promise<AuthIdentity> {
  return request(authConnection, '/api/projects', {
    method: 'POST',
    body: { project_id: projectId },
    schema: authIdentitySchema,
  })
}

export function getAdminSession(): Promise<AuthIdentity> {
  return request(authConnection, '/api/auth/me', {
    schema: authIdentitySchema,
    redirectOnUnauthorized: false,
  })
}

export function logoutAdmin(): Promise<unknown> {
  return request(authConnection, '/api/auth/logout', {
    method: 'POST',
    redirectOnUnauthorized: false,
  })
}
