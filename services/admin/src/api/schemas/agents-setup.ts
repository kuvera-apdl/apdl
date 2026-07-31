import { z } from 'zod'

export const agentsProjectIdSchema = z.string().regex(/^[A-Za-z0-9]{1,64}$/)
export const llmProviderSchema = z.enum(['openai', 'anthropic', 'google', 'xai'])
export const llmModelTierSchema = z.enum(['fast', 'reasoning'])
export const llmDataClassificationSchema = z.enum([
  'public',
  'internal',
  'confidential',
  'restricted',
])
export const llmCatalogVersionSchema = z
  .string()
  .regex(/^llm-provider-catalog@[1-9][0-9]*$/)

const canonicalTiersSchema = z
  .array(llmModelTierSchema)
  .min(1)
  .max(2)
  .superRefine((tiers, context) => {
    const canonical = ['fast', 'reasoning'].filter((tier) => tiers.includes(tier as 'fast' | 'reasoning'))
    if (
      tiers.length !== canonical.length ||
      tiers.some((tier, index) => tier !== canonical[index])
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'supported_tiers must be unique and use canonical order',
      })
    }
  })

const canonicalClassificationsSchema = z
  .array(llmDataClassificationSchema)
  .min(1)
  .max(4)
  .superRefine((classifications, context) => {
    const order = ['public', 'internal', 'confidential', 'restricted'] as const
    const canonical = order.filter((item) => classifications.includes(item))
    if (
      classifications.length !== canonical.length ||
      classifications.some((item, index) => item !== canonical[index])
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        message: 'allowed_data_classifications must be unique and use canonical order',
      })
    }
  })

export const llmProviderModelSchema = z
  .object({
    schema_version: z.literal('llm_provider_model@1'),
    provider: llmProviderSchema,
    model_id: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    display_name: z.string().trim().min(1).max(200),
    supported_tiers: canonicalTiersSchema,
    catalog_version: llmCatalogVersionSchema,
    data_residency: z.enum(['ca', 'us', 'eu', 'global']),
    allowed_data_classifications: canonicalClassificationsSchema,
    endpoint_host: z
      .string()
      .min(1)
      .max(253)
      .regex(/^[A-Za-z0-9.-]+$/),
    input_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    output_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    pricing_status: z.literal('catalog_reviewed'),
  })
  .strict()

const llmConnectionSummaryShape = {
  schema_version: z.literal('llm_provider_connection@1'),
  project_id: agentsProjectIdSchema,
  provider: llmProviderSchema,
  version: z.number().int().positive(),
  inventory_version: z.number().int().positive(),
  state: z.enum(['active', 'revoked']),
  catalog_version: llmCatalogVersionSchema,
  validated_at: z.string().datetime({ offset: true }),
  created_at: z.string().datetime({ offset: true }),
  updated_at: z.string().datetime({ offset: true }),
  revoked_at: z.string().datetime({ offset: true }).nullable(),
  model_count: z.number().int().min(0).max(1_000),
} as const

type ConnectionShape = {
  state: 'active' | 'revoked'
  revoked_at: string | null
  model_count: number
}

function validateConnectionShape(
  connection: ConnectionShape,
  context: z.RefinementCtx,
): void {
  if ((connection.state === 'revoked') !== (connection.revoked_at !== null)) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['revoked_at'],
      message: 'revoked_at must be present exactly for a revoked connection',
    })
  }
  if (connection.state === 'active' && connection.model_count < 1) {
    context.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['model_count'],
      message: 'an active connection must contain a model inventory',
    })
  }
}

export const llmConnectionSummarySchema = z
  .object(llmConnectionSummaryShape)
  .strict()
  .superRefine(validateConnectionShape)

export const llmConnectionDetailSchema = z
  .object({
    ...llmConnectionSummaryShape,
    models: z.array(llmProviderModelSchema).min(1).max(1_000),
  })
  .strict()
  .superRefine((connection, context) => {
    validateConnectionShape(connection, context)
    if (connection.models.length !== connection.model_count) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['models'],
        message: 'models must match model_count',
      })
    }
    const identifiers = new Set<string>()
    connection.models.forEach((model, index) => {
      if (model.provider !== connection.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match its connection',
        })
      }
      if (identifiers.has(model.model_id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'model_id'],
          message: 'model identifiers must be unique',
        })
      }
      identifiers.add(model.model_id)
    })
  })

export const llmConnectionListSchema = z
  .object({
    schema_version: z.literal('llm_provider_connection_list@1'),
    project_id: agentsProjectIdSchema,
    connections: z.array(llmConnectionSummarySchema).max(4),
  })
  .strict()
  .superRefine((value, context) => {
    const providers = new Set<string>()
    value.connections.forEach((connection, index) => {
      if (connection.project_id !== value.project_id) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['connections', index, 'project_id'],
          message: 'connection project must match the list project',
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
    project_id: agentsProjectIdSchema,
    provider: llmProviderSchema,
    connection_version: z.number().int().positive(),
    inventory_version: z.number().int().positive(),
    models: z.array(llmProviderModelSchema).min(1).max(1_000),
  })
  .strict()
  .superRefine((inventory, context) => {
    const identifiers = new Set<string>()
    inventory.models.forEach((model, index) => {
      if (model.provider !== inventory.provider) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'provider'],
          message: 'model provider must match the inventory provider',
        })
      }
      if (identifiers.has(model.model_id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['models', index, 'model_id'],
          message: 'model identifiers must be unique',
        })
      }
      identifiers.add(model.model_id)
    })
  })

export const putLlmConnectionRequestSchema = z
  .object({
    project_id: agentsProjectIdSchema,
    api_key: z.string().min(1).max(16_384),
    version: z.number().int().nonnegative(),
  })
  .strict()
  .superRefine((value, context) => {
    if (new TextEncoder().encode(value.api_key).byteLength > 16_384) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['api_key'],
        message: 'api_key must not exceed 16384 UTF-8 bytes',
      })
    }
  })

export const refreshLlmConnectionRequestSchema = z
  .object({
    project_id: agentsProjectIdSchema,
    version: z.number().int().positive(),
  })
  .strict()

export const revokeLlmConnectionRequestSchema = refreshLlmConnectionRequestSchema
  .extend({
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

export const providerDiscoveryErrorCodeSchema = z.enum([
  'invalid_key',
  'permission_denied',
  'rate_limited',
  'provider_timeout',
  'provider_unavailable',
  'malformed_response',
  'no_supported_models',
])

export const providerDiscoveryErrorBodySchema = z
  .object({
    detail: z
      .object({
        code: providerDiscoveryErrorCodeSchema,
        message: z.string().min(1),
      })
      .strict(),
  })
  .strict()

export const agentsSetupBlockerSchema = z.enum([
  'project_inactive',
  'fast_model_required',
  'reasoning_model_required',
  'connection_inactive',
  'connection_stale',
  'inventory_stale',
  'model_unavailable',
  'model_ineligible',
  'catalog_stale',
  'credential_unavailable',
  'budget_invalid',
])

export const agentsModelSelectionSchema = z
  .object({
    provider: llmProviderSchema,
    model: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    connection_version: z.number().int().positive(),
    inventory_version: z.number().int().positive(),
  })
  .strict()

export const putAgentsSetupRequestSchema = z
  .object({
    project_id: agentsProjectIdSchema,
    fast_model: agentsModelSelectionSchema,
    reasoning_model: agentsModelSelectionSchema,
    version: z.number().int().nonnegative(),
  })
  .strict()

export const deactivateAgentsSetupRequestSchema = z
  .object({
    project_id: agentsProjectIdSchema,
    version: z.number().int().positive(),
    reason: z
      .string()
      .min(1)
      .max(2_000)
      .refine(
        (reason) =>
          reason === reason.trim() &&
          !reason.includes('\r') &&
          !reason.includes('\n'),
        'reason must not contain surrounding whitespace or line breaks',
      ),
  })
  .strict()

export const agentsSetupAssignmentSchema = z
  .object({
    tier: llmModelTierSchema,
    provider: llmProviderSchema,
    model: z.string().regex(/^[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}$/),
    connection_version: z.number().int().positive(),
    inventory_version: z.number().int().positive(),
    model_catalog_version: llmCatalogVersionSchema,
    display_name: z.string().min(1),
    endpoint_url: z.string().url(),
    endpoint_host: z.string().min(1),
    data_residency: z.enum(['ca', 'us', 'eu', 'global']),
    allowed_data_classifications: canonicalClassificationsSchema,
    input_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    output_cost_per_million_tokens_usd_micros: z.number().int().nonnegative(),
    current: z.boolean(),
    assigned_at: z.string().datetime({ offset: true }),
    updated_at: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((assignment, context) => {
    try {
      if (new URL(assignment.endpoint_url).hostname !== assignment.endpoint_host) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['endpoint_host'],
          message: 'endpoint host must match the reviewed endpoint URL',
        })
      }
    } catch {
      // endpoint_url reports its own schema issue.
    }
  })

export const agentsSetupConnectionSchema = z
  .object({
    provider: llmProviderSchema,
    connection_version: z.number().int().positive(),
    inventory_version: z.number().int().positive(),
    state: z.enum(['active', 'revoked']),
    catalog_version: llmCatalogVersionSchema,
    current: z.boolean(),
    validated_at: z.string().datetime({ offset: true }),
  })
  .strict()
  .superRefine((connection, context) => {
    if (connection.current && connection.state !== 'active') {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['current'],
        message: 'only active connections can be current',
      })
    }
  })

export const agentsSetupCallerCapabilitiesSchema = z
  .object({
    can_read: z.literal(true),
    can_manage: z.boolean(),
    can_activate: z.boolean(),
    can_deactivate: z.boolean(),
    management_authority: z.enum(['owner', 'delegated', 'none']),
  })
  .strict()
  .superRefine((capabilities, context) => {
    if (capabilities.can_manage !== (capabilities.management_authority !== 'none')) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['can_manage'],
        message: 'management authority and can_manage must agree',
      })
    }
  })

export const agentsSetupPolicySchema = z
  .object({
    required_data_residency: z.enum(['local', 'ca', 'us', 'eu', 'global']),
    allow_cross_vendor_retry: z.literal(false),
    project_daily_cost_limit_usd_micros: z.number().int().nonnegative(),
    run_cost_limit_usd_micros: z.number().int().nonnegative(),
  })
  .strict()

export const effectfulExecutionSchema = z
  .object({
    authorized: z.boolean(),
    authorization_source: z
      .enum(['operator_provisioned', 'self_registered_override'])
      .nullable(),
  })
  .strict()
  .superRefine((effectful, context) => {
    if (effectful.authorized !== (effectful.authorization_source !== null)) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['authorization_source'],
        message: 'authorization source must be present exactly when authorized',
      })
    }
  })

const agentsSetupResponseShape = {
  schema_version: z.literal('agents_project_setup@1'),
  project_id: agentsProjectIdSchema,
  version: z.number().int().nonnegative(),
  caller_capabilities: agentsSetupCallerCapabilitiesSchema,
  assignments: z.array(agentsSetupAssignmentSchema).max(2),
  connections: z.array(agentsSetupConnectionSchema).max(4),
  blockers: z.array(agentsSetupBlockerSchema).max(11),
  analysis_ready: z.boolean(),
  policy: agentsSetupPolicySchema,
  effectful_execution: effectfulExecutionSchema,
  activated_at: z.string().datetime({ offset: true }).nullable(),
  deactivated_at: z.string().datetime({ offset: true }).nullable(),
  deactivation_reason: z.string().min(1).max(2_000).nullable(),
} as const

export const agentsSetupResponseSchema = z
  .discriminatedUnion('state', [
    z
      .object({
        ...agentsSetupResponseShape,
        state: z.literal('inactive'),
      })
      .strict(),
    z
      .object({
        ...agentsSetupResponseShape,
        state: z.literal('active'),
      })
      .strict(),
  ])
  .superRefine((setup, context) => {
    const assignmentTiers = new Set<string>()
    setup.assignments.forEach((assignment, index) => {
      if (assignmentTiers.has(assignment.tier)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['assignments', index, 'tier'],
          message: 'assignment tiers must be unique',
        })
      }
      assignmentTiers.add(assignment.tier)
    })

    const connectionProviders = new Set<string>()
    setup.connections.forEach((connection, index) => {
      if (connectionProviders.has(connection.provider)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['connections', index, 'provider'],
          message: 'setup connection providers must be unique',
        })
      }
      connectionProviders.add(connection.provider)
    })

    if (new Set(setup.blockers).size !== setup.blockers.length) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['blockers'],
        message: 'setup blockers must be unique',
      })
    }

    const shouldBeReady = setup.state === 'active' && setup.blockers.length === 0
    if (setup.analysis_ready !== shouldBeReady) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['analysis_ready'],
        message: 'analysis readiness must reflect active state without blockers',
      })
    }
    if (setup.state === 'active') {
      if (setup.activated_at === null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['activated_at'],
          message: 'active setup must include its activation timestamp',
        })
      }
      if (setup.deactivated_at !== null || setup.deactivation_reason !== null) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['deactivated_at'],
          message: 'active setup cannot include deactivation state',
        })
      }
    } else {
      if (!setup.blockers.includes('project_inactive')) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['blockers'],
          message: 'inactive setup must include project_inactive',
        })
      }
      if (
        (setup.deactivated_at === null) !==
        (setup.deactivation_reason === null)
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['deactivation_reason'],
          message: 'deactivation timestamp and reason must appear together',
        })
      }
      if (
        setup.deactivated_at !== null &&
        setup.activated_at === null
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['activated_at'],
          message: 'deactivated setup must retain its activation timestamp',
        })
      }
    }
    const budgetsValid =
      setup.policy.project_daily_cost_limit_usd_micros > 0 &&
      setup.policy.run_cost_limit_usd_micros > 0 &&
      setup.policy.run_cost_limit_usd_micros <=
        setup.policy.project_daily_cost_limit_usd_micros
    if (setup.blockers.includes('budget_invalid') === budgetsValid) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['policy'],
        message: 'budget_invalid must exactly reflect the project cost limits',
      })
    }
    if (setup.analysis_ready) {
      if (
        setup.assignments.length !== 2 ||
        !assignmentTiers.has('fast') ||
        !assignmentTiers.has('reasoning')
      ) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['assignments'],
          message: 'ready setup requires one fast and one reasoning assignment',
        })
      }
      setup.assignments.forEach((assignment, index) => {
        if (!assignment.current) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['assignments', index, 'current'],
            message: 'ready setup assignments must be current',
          })
        }
        if (
          assignment.data_residency !==
          setup.policy.required_data_residency
        ) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['assignments', index, 'data_residency'],
            message: 'assignment residency must match the active policy',
          })
        }
        const connection = setup.connections.find(
          (candidate) =>
            candidate.provider === assignment.provider &&
            candidate.connection_version === assignment.connection_version &&
            candidate.inventory_version === assignment.inventory_version &&
            candidate.catalog_version === assignment.model_catalog_version,
        )
        if (
          !connection ||
          connection.state !== 'active' ||
          !connection.current
        ) {
          context.addIssue({
            code: z.ZodIssueCode.custom,
            path: ['connections'],
            message:
              'every ready assignment must reference its exact current connection',
          })
        }
      })
    }
    if (
      setup.caller_capabilities.can_activate !==
      (setup.caller_capabilities.can_manage && setup.state === 'inactive')
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['caller_capabilities', 'can_activate'],
        message: 'activation capability must reflect setup state and authority',
      })
    }
    if (
      setup.caller_capabilities.can_deactivate !==
      (setup.caller_capabilities.can_manage && setup.state === 'active')
    ) {
      context.addIssue({
        code: z.ZodIssueCode.custom,
        path: ['caller_capabilities', 'can_deactivate'],
        message: 'deactivation capability must reflect setup state and authority',
      })
    }
  })
