import { z } from 'zod'

import { request, type ServiceConnection } from '@/api/http'

export const llmProviderSchema = z.enum(['anthropic', 'openai', 'google', 'xai'])
export const llmConsumerSchema = z.enum(['agents', 'codegen'])

const projectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)
const timestampSchema = z.string().datetime({ offset: true })
const connectionIdSchema = z.string().uuid()

const vaultModelSchema = z
  .object({
    schema_version: z.literal('project_llm_provider_model@1'),
    model_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
  })
  .strict()

const connectionSummaryObjectSchema = z
  .object({
    schema_version: z.literal('project_llm_connection@1'),
    connection_id: connectionIdSchema,
    project_id: projectIdSchema,
    provider: llmProviderSchema,
    label: z.string().min(1).max(80),
    version: z.number().int().min(1),
    inventory_version: z.number().int().min(1),
    state: z.enum(['active', 'revoked']),
    consumers: z.array(llmConsumerSchema).min(0).max(2),
    validated_at: timestampSchema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
    revoked_at: timestampSchema.nullable(),
    model_count: z.number().int().min(0).max(1_000),
  })
  .strict()

function validateConnectionState(
  connection: z.infer<typeof connectionSummaryObjectSchema>,
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
  connectionSummaryObjectSchema.superRefine(validateConnectionState)

export const llmConnectionDetailSchema = connectionSummaryObjectSchema
  .extend({ models: z.array(vaultModelSchema) })
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
  })

export const llmConnectionListSchema = z
  .object({
    schema_version: z.literal('project_llm_connection_list@1'),
    project_id: projectIdSchema,
    connections: z.array(llmConnectionSummarySchema),
  })
  .strict()

const consumersSchema = z
  .array(llmConsumerSchema)
  .min(1)
  .max(2)
  .refine((items) => new Set(items).size === items.length, 'consumers must be unique')
  .refine(
    (items) => items.join(',') === ['agents', 'codegen'].filter((item) => items.includes(item as LlmConsumer)).join(','),
    'consumers must use canonical agents, codegen order',
  )

const connectionWriteSchema = z
  .object({
    project_id: projectIdSchema,
    provider: llmProviderSchema,
    label: z.string().min(1).max(80).refine((value) => value === value.trim()),
    api_key: z.string().min(1).max(16_384),
    consumers: consumersSchema,
  })
  .strict()

const replaceConnectionSchema = connectionWriteSchema
  .extend({ version: z.number().int().min(1) })
  .strict()

const versionSchema = z
  .object({ project_id: projectIdSchema, version: z.number().int().min(1) })
  .strict()

const revokeSchema = versionSchema
  .extend({
    reason: z
      .string()
      .min(1)
      .max(2_000)
      .refine((value) => value === value.trim() && !/[\r\n]/.test(value)),
  })
  .strict()

export type LlmProvider = z.infer<typeof llmProviderSchema>
export type LlmConsumer = z.infer<typeof llmConsumerSchema>
export type LlmConnectionSummary = z.infer<typeof llmConnectionSummarySchema>
export type LlmConnectionDetail = z.infer<typeof llmConnectionDetailSchema>
export type LlmConnectionList = z.infer<typeof llmConnectionListSchema>

function path(connectionId?: string): string {
  const base = '/v1/llm-connections'
  return connectionId ? `${base}/${encodeURIComponent(connectionId)}` : base
}

function checkedProject<T extends { project_id: string }>(
  value: T,
  projectId: string,
): T {
  if (value.project_id !== projectId) {
    throw new Error('LLM vault response crossed project authority')
  }
  return value
}

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

export function listLlmConnections(
  connection: ServiceConnection,
  projectId: string,
  signal?: AbortSignal,
): Promise<LlmConnectionList> {
  return request(connection, path(), {
    query: { project_id: projectId },
    signal,
    schema: llmConnectionListSchema,
  }).then((value) => checkedProject(value, projectId))
}

export function getLlmConnection(
  connection: ServiceConnection,
  connectionId: string,
  projectId: string,
  signal?: AbortSignal,
): Promise<LlmConnectionDetail> {
  return request(connection, path(connectionId), {
    query: { project_id: projectId },
    signal,
    schema: llmConnectionDetailSchema,
  }).then((value) => checkedProject(value, projectId))
}

export function createLlmConnection(
  connection: ServiceConnection,
  projectId: string,
  provider: LlmProvider,
  label: string,
  apiKey: string,
  consumers: LlmConsumer[],
): Promise<LlmConnectionDetail> {
  const body = connectionWriteSchema.parse({
    project_id: projectId,
    provider,
    label,
    api_key: apiKey,
    consumers,
  })
  return request(connection, path(), {
    method: 'POST',
    body,
    schema: llmConnectionDetailSchema,
    redirectOnUnauthorized: (responseBody) =>
      !isProviderCredentialRejection(responseBody),
  }).then((value) => checkedProject(value, projectId))
}

export function replaceLlmConnection(
  connection: ServiceConnection,
  current: LlmConnectionSummary,
  label: string,
  apiKey: string,
  consumers: LlmConsumer[],
): Promise<LlmConnectionDetail> {
  const body = replaceConnectionSchema.parse({
    project_id: current.project_id,
    provider: current.provider,
    label,
    api_key: apiKey,
    consumers,
    version: current.version,
  })
  return request(connection, path(current.connection_id), {
    method: 'PUT',
    body,
    schema: llmConnectionDetailSchema,
    redirectOnUnauthorized: (responseBody) =>
      !isProviderCredentialRejection(responseBody),
  }).then((value) => checkedProject(value, current.project_id))
}

export function refreshLlmConnection(
  connection: ServiceConnection,
  current: LlmConnectionSummary,
): Promise<LlmConnectionDetail> {
  const body = versionSchema.parse({
    project_id: current.project_id,
    version: current.version,
  })
  return request(connection, `${path(current.connection_id)}/refresh`, {
    method: 'POST',
    body,
    schema: llmConnectionDetailSchema,
    redirectOnUnauthorized: (responseBody) =>
      !isProviderCredentialRejection(responseBody),
  }).then((value) => checkedProject(value, current.project_id))
}

export function revokeLlmConnection(
  connection: ServiceConnection,
  current: LlmConnectionSummary,
  reason: string,
): Promise<LlmConnectionSummary> {
  const body = revokeSchema.parse({
    project_id: current.project_id,
    version: current.version,
    reason,
  })
  return request(connection, `${path(current.connection_id)}/revoke`, {
    method: 'POST',
    body,
    schema: llmConnectionSummarySchema,
  }).then((value) => checkedProject(value, current.project_id))
}
