import { z } from 'zod'

export const codegenLlmProviderSchema = z.enum(['openai', 'anthropic', 'google', 'xai'])
export const codegenLlmRoleSchema = z.enum(['editor', 'helper'])
export const codegenLlmProjectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)

const timestampSchema = z.string().datetime({ offset: true })
const dataClassificationSchema = z.enum([
  'public',
  'internal',
  'confidential',
  'restricted',
])

export const codegenLlmProviderModelSchema = z
  .object({
    schema_version: z.literal('codegen_provider_model@1'),
    provider: codegenLlmProviderSchema,
    model_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    display_name: z.string().trim().min(1),
    supported_roles: z.array(codegenLlmRoleSchema).min(1).max(2),
    catalog_version: z.string().trim().min(1),
    context_window_tokens: z.number().int().positive(),
    supports_tool_calling: z.boolean(),
    supports_structured_output: z.boolean(),
    data_residency: z.enum(['ca', 'us', 'eu', 'global']),
    allowed_data_classifications: z.array(dataClassificationSchema).min(1).max(4),
    input_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    output_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    pricing_status: z.literal('catalog_reviewed'),
  })
  .strict()
  .superRefine((model, context) => {
    const roles = new Set(model.supported_roles)
    if (roles.size !== model.supported_roles.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['supported_roles'],
        message: 'supported_roles must be unique',
      })
    }
    const classifications = new Set(model.allowed_data_classifications)
    if (classifications.size !== model.allowed_data_classifications.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['allowed_data_classifications'],
        message: 'allowed_data_classifications must be unique',
      })
    }
  })

const codegenLlmConnectionSummaryObjectSchema = z
  .object({
    schema_version: z.literal('codegen_provider_connection@1'),
    project_id: codegenLlmProjectIdSchema,
    provider: codegenLlmProviderSchema,
    version: z.number().int().min(1),
    inventory_version: z.number().int().min(1),
    state: z.enum(['active', 'revoked']),
    catalog_version: z.string().trim().min(1),
    validated_at: timestampSchema,
    created_at: timestampSchema,
    updated_at: timestampSchema,
    revoked_at: timestampSchema.nullable(),
    model_count: z.number().int().min(0).max(1_000),
  })
  .strict()

function validateConnectionState(
  connection: z.infer<typeof codegenLlmConnectionSummaryObjectSchema>,
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
  if (connection.state === 'active' && connection.model_count < 1) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['model_count'],
      message: 'active connections require at least one model',
    })
  }
  if (connection.state === 'revoked' && connection.model_count !== 0) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['model_count'],
      message: 'revoked connections cannot retain models',
    })
  }
}

export const codegenLlmConnectionSummarySchema =
  codegenLlmConnectionSummaryObjectSchema.superRefine(validateConnectionState)

export const codegenLlmConnectionDetailSchema = codegenLlmConnectionSummaryObjectSchema
  .extend({
    models: z.array(codegenLlmProviderModelSchema).max(1_000),
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
    const modelIds = new Set<string>()
    connection.models.forEach((model, index) => {
      if (model.provider !== connection.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match connection provider',
        })
      }
      if (model.catalog_version !== connection.catalog_version) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'catalog_version'],
          message: 'model catalog version must match connection catalog version',
        })
      }
      if (modelIds.has(model.model_id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'model_id'],
          message: 'model identifiers must be unique',
        })
      }
      modelIds.add(model.model_id)
    })
  })

export const codegenLlmConnectionListSchema = z
  .object({
    schema_version: z.literal('codegen_provider_connection_list@1'),
    project_id: codegenLlmProjectIdSchema,
    connections: z.array(codegenLlmConnectionSummarySchema).max(4),
  })
  .strict()
  .superRefine((list, context) => {
    const providers = new Set<z.infer<typeof codegenLlmProviderSchema>>()
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

export const codegenLlmModelInventorySchema = z
  .object({
    schema_version: z.literal('codegen_provider_model_inventory@1'),
    project_id: codegenLlmProjectIdSchema,
    provider: codegenLlmProviderSchema,
    connection_version: z.number().int().min(1),
    inventory_version: z.number().int().min(1),
    models: z.array(codegenLlmProviderModelSchema).min(1).max(1_000),
  })
  .strict()
  .superRefine((inventory, context) => {
    const modelIds = new Set<string>()
    inventory.models.forEach((model, index) => {
      if (model.provider !== inventory.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match inventory provider',
        })
      }
      if (modelIds.has(model.model_id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'model_id'],
          message: 'model identifiers must be unique',
        })
      }
      modelIds.add(model.model_id)
    })
  })

export const putCodegenLlmConnectionRequestSchema = z
  .object({
    project_id: codegenLlmProjectIdSchema,
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

export const refreshCodegenLlmConnectionRequestSchema = z
  .object({
    project_id: codegenLlmProjectIdSchema,
    version: z.number().int().min(1),
  })
  .strict()

export const revokeCodegenLlmConnectionRequestSchema = z
  .object({
    project_id: codegenLlmProjectIdSchema,
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
