import { z } from 'zod'

import { request, type ServiceConnection } from '@/api/http'

export const llmProviderSchema = z.enum(['openai', 'anthropic', 'google', 'xai'])
export const llmModelTierSchema = z.enum(['fast', 'reasoning'])

const projectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)
const timestampSchema = z.string().datetime({ offset: true })

export const llmProviderModelSchema = z
  .object({
    schema_version: z.literal('llm_provider_model@1'),
    provider: llmProviderSchema,
    model_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    display_name: z.string(),
    supported_tiers: z.array(llmModelTierSchema),
    catalog_version: z.string(),
    data_residency: z.enum(['ca', 'us', 'eu', 'global']),
    allowed_data_classifications: z.array(
      z.enum(['public', 'internal', 'confidential', 'restricted']),
    ),
    pricing_status: z.literal('operator_review_required'),
  })
  .strict()

const llmConnectionSummaryObjectSchema = z
  .object({
    schema_version: z.literal('llm_provider_connection@1'),
    project_id: projectIdSchema,
    provider: llmProviderSchema,
    version: z.number().int().min(1),
    state: z.enum(['active', 'revoked']),
    catalog_version: z.string(),
    validated_at: timestampSchema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
    revoked_at: timestampSchema.nullable(),
    model_count: z.number().int().min(0).max(1_000),
  })
  .strict()

function validateConnectionState(
  connection: z.infer<typeof llmConnectionSummaryObjectSchema>,
  context: z.RefinementCtx,
): void {
  if (connection.state === 'active' && connection.revoked_at !== null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['revoked_at'],
      message: 'active connections cannot have revoked_at',
    })
  }
  if (connection.state === 'revoked' && connection.revoked_at === null) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['revoked_at'],
      message: 'revoked connections require revoked_at',
    })
  }
}

export const llmConnectionSummarySchema =
  llmConnectionSummaryObjectSchema.superRefine(validateConnectionState)

export const llmConnectionDetailSchema = llmConnectionSummaryObjectSchema
  .extend({
    models: z.array(llmProviderModelSchema),
  })
  .strict()
  .superRefine((connection, context) => {
    validateConnectionState(connection, context)
    if (connection.model_count !== connection.models.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['model_count'],
        message: 'model_count must match models',
      })
    }
    connection.models.forEach((model, index) => {
      if (model.provider !== connection.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match connection provider',
        })
      }
    })
  })

export const llmConnectionListSchema = z
  .object({
    schema_version: z.literal('llm_provider_connection_list@1'),
    project_id: projectIdSchema,
    connections: z.array(llmConnectionSummarySchema),
  })
  .strict()
  .superRefine((list, context) => {
    const providers = new Set<LlmProvider>()
    list.connections.forEach((connection, index) => {
      if (connection.project_id !== list.project_id) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['connections', index, 'project_id'],
          message: 'connection project must match list project',
        })
      }
      if (providers.has(connection.provider)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['connections', index, 'provider'],
          message: 'connection providers must be unique',
        })
      }
      providers.add(connection.provider)
    })
  })

export const llmModelInventorySchema = z
  .object({
    schema_version: z.literal('llm_provider_model_inventory@1'),
    project_id: projectIdSchema,
    provider: llmProviderSchema,
    connection_version: z.number().int().min(1),
    models: z.array(llmProviderModelSchema),
  })
  .strict()
  .superRefine((inventory, context) => {
    inventory.models.forEach((model, index) => {
      if (model.provider !== inventory.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match inventory provider',
        })
      }
    })
  })

export const putLlmConnectionRequestSchema = z
  .object({
    project_id: projectIdSchema,
    api_key: z.string().min(1).max(16_384),
    version: z.number().int().min(0),
  })
  .strict()
  .superRefine((body, context) => {
    if (new TextEncoder().encode(body.api_key).length > 16_384) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['api_key'],
        message: 'api_key must not exceed 16384 UTF-8 bytes',
      })
    }
  })

export const refreshLlmConnectionRequestSchema = z
  .object({
    project_id: projectIdSchema,
    version: z.number().int().min(1),
  })
  .strict()

export const revokeLlmConnectionRequestSchema = z
  .object({
    project_id: projectIdSchema,
    version: z.number().int().min(1),
    reason: z
      .string()
      .min(1)
      .max(2_000)
      .refine(
        (reason) =>
          reason === reason.trim() && !reason.includes('\r') && !reason.includes('\n'),
        'reason must not contain surrounding whitespace or line breaks',
      ),
  })
  .strict()

export type LlmProvider = z.infer<typeof llmProviderSchema>
export type LlmProviderModel = z.infer<typeof llmProviderModelSchema>
export type LlmConnectionSummary = z.infer<typeof llmConnectionSummarySchema>
export type LlmConnectionDetail = z.infer<typeof llmConnectionDetailSchema>
export type LlmConnectionList = z.infer<typeof llmConnectionListSchema>
export type LlmModelInventory = z.infer<typeof llmModelInventorySchema>

function isProviderCredentialRejection(body: unknown): boolean {
  if (typeof body !== 'object' || body === null || !('detail' in body)) return false
  const detail = body.detail
  return (
    typeof detail === 'object' &&
    detail !== null &&
    'code' in detail &&
    detail.code === 'invalid_key' &&
    'message' in detail &&
    typeof detail.message === 'string'
  )
}

function connectionPath(provider?: LlmProvider): string {
  const base = '/v1/agents/llm-connections'
  return provider ? `${base}/${provider}` : base
}

export function listLlmConnections(
  connection: ServiceConnection,
  projectId: string,
  signal?: AbortSignal,
): Promise<LlmConnectionList> {
  return request(connection, connectionPath(), {
    query: { project_id: projectId },
    signal,
    schema: llmConnectionListSchema,
  }).then((result) => {
    if (result.project_id !== projectId) {
      throw new Error('LLM connection list crossed project authority')
    }
    return result
  })
}

export function putLlmConnection(
  connection: ServiceConnection,
  provider: LlmProvider,
  projectId: string,
  apiKey: string,
  version: number,
): Promise<LlmConnectionDetail> {
  const body = putLlmConnectionRequestSchema.parse({
    project_id: projectId,
    api_key: apiKey,
    version,
  })
  return request(connection, connectionPath(provider), {
    method: 'PUT',
    body,
    schema: llmConnectionDetailSchema,
    // Suppress sign-out only for the canonical provider-auth rejection. A BFF
    // session 401 must still terminate the Admin session.
    redirectOnUnauthorized: (body) => !isProviderCredentialRejection(body),
  }).then((result) => {
    if (result.project_id !== projectId || result.provider !== provider) {
      throw new Error('LLM connection response crossed project authority')
    }
    return result
  })
}

export function getLlmModels(
  connection: ServiceConnection,
  provider: LlmProvider,
  projectId: string,
  signal?: AbortSignal,
): Promise<LlmModelInventory> {
  return request(connection, `${connectionPath(provider)}/models`, {
    query: { project_id: projectId },
    signal,
    schema: llmModelInventorySchema,
  }).then((result) => {
    if (result.project_id !== projectId || result.provider !== provider) {
      throw new Error('LLM model inventory crossed project authority')
    }
    return result
  })
}

export function refreshLlmModels(
  connection: ServiceConnection,
  provider: LlmProvider,
  projectId: string,
  version: number,
): Promise<LlmConnectionDetail> {
  const body = refreshLlmConnectionRequestSchema.parse({
    project_id: projectId,
    version,
  })
  return request(connection, `${connectionPath(provider)}/refresh-models`, {
    method: 'POST',
    body,
    schema: llmConnectionDetailSchema,
    // Refresh can expose the same provider-auth rejection as connect.
    redirectOnUnauthorized: (body) => !isProviderCredentialRejection(body),
  }).then((result) => {
    if (result.project_id !== projectId || result.provider !== provider) {
      throw new Error('LLM model refresh crossed project authority')
    }
    return result
  })
}

export function revokeLlmConnection(
  connection: ServiceConnection,
  provider: LlmProvider,
  projectId: string,
  version: number,
  reason: string,
): Promise<LlmConnectionSummary> {
  const body = revokeLlmConnectionRequestSchema.parse({
    project_id: projectId,
    version,
    reason,
  })
  return request(connection, `${connectionPath(provider)}/revoke`, {
    method: 'POST',
    body,
    schema: llmConnectionSummarySchema,
  }).then((result) => {
    if (result.project_id !== projectId || result.provider !== provider) {
      throw new Error('LLM connection revocation crossed project authority')
    }
    return result
  })
}
