import { z } from 'zod'

import { request } from '@/api/http'

export const authIdentitySchema = z
  .object({
    credential_id: z.string().min(1),
    project_id: z.string().min(1),
    roles: z.array(z.string().min(1)).min(1),
  })
  .strict()

export type AuthIdentity = z.infer<typeof authIdentitySchema>

/** Verify an API key without triggering the global expired-session redirect. */
export function authenticateAdmin(configUrl: string, apiKey: string): Promise<AuthIdentity> {
  return request(
    { baseUrl: configUrl, apiKey, actor: '' },
    '/v1/auth/me',
    {
      schema: authIdentitySchema,
      redirectOnUnauthorized: false,
    },
  )
}
