// Agents-service mirrors (routers/triggers.py, status.py, approvals.py).
import { z } from 'zod'

export const analysisTypeSchema = z.enum([
  'behavior_analysis',
  'experiment_design',
  'personalization',
  'feature_proposal',
])

export const triggerTypeSchema = z.enum(['scheduled', 'manual', 'threshold_alert'])

export const triggerRequestSchema = z
  .object({
    project_id: z.string().min(1),
    trigger_type: triggerTypeSchema,
    // Plain strings, not the built-in enum: project-scoped custom agent slugs
    // are valid analysis types too (the server validates against registry+DB).
    analysis_types: z.array(z.string().min(1)).min(1),
    time_range_days: z.number().int().min(1).max(90),
    autonomy_level: z.number().int().min(1).max(4),
  })
  .strict()

export const triggerResponseSchema = z
  .object({
    run_id: z.string().min(1),
    status: z.literal('started'),
  })
  .strict()

export const projectExecutionCapabilitiesSchema = z
  .object({
    schema_version: z.literal('agents_project_execution_capabilities@1'),
    project_id: z.string().regex(/^[A-Za-z0-9]{1,64}$/),
    autonomous_mutations_operator_enabled: z.boolean(),
    codegen_changeset_creation: z.enum(['available', 'disabled', 'unavailable']),
  })
  .strict()

// Run lifecycle vocabularies (supervisor.py); status/phase are plain strings
// in the server model, so the response schema stays string-typed and the UI
// maps known values.
export const KNOWN_RUN_STATUSES = [
  'started',
  'running',
  'waiting_approval',
  'approval_queued',
  'approved',
  'rejected',
  'completed',
  'completed_with_errors',
  'manual_intervention',
  'failed',
  'cancelled',
] as const

export const TERMINAL_RUN_STATUSES = new Set<string>([
  'completed',
  'completed_with_errors',
  'manual_intervention',
  'failed',
  'cancelled',
])

export const runStatusSchema = z
  .object({
    run_id: z.string(),
    project_id: z.string(),
    status: z.string(),
    phase: z.string(),
    insights_count: z.number().int(),
    experiments_count: z.number().int(),
    started_at: z.string(),
    updated_at: z.string(),
    // The run's trigger inputs, surfaced by the server so the console no longer
    // caches them in localStorage. Optional so an older backend that omits them
    // still parses under .strict(); consumers treat absence as "unknown".
    trigger_type: z.string().optional(),
    autonomy_level: z.number().int().nullable().optional(),
    analysis_types: z.array(z.string()).optional(),
  })
  .strict()

export const itemDecisionSchema = z
  .object({
    item_id: z
      .string()
      .min(1)
      .max(128)
      .refine((value) => value.trim() === value && value.length > 0, {
        message: 'item_id must be a bounded non-whitespace identifier',
      }),
    approved: z.boolean(),
  })
  .strict()

// One strict decision per persisted gate item. Whole-gate verdicts are
// intentionally unsupported: they made missing and duplicate identities
// ambiguous at the mutation boundary.
export const approvalRequestSchema = z
  .object({
    decisions: z.array(itemDecisionSchema).min(1).max(100),
    comment: z.string().max(2_000).optional(),
  })
  .strict()
  .superRefine((value, context) => {
    const seen = new Set<string>()
    value.decisions.forEach((decision, index) => {
      if (seen.has(decision.item_id)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['decisions', index, 'item_id'],
          message: `duplicate decision for ${decision.item_id}`,
        })
      }
      seen.add(decision.item_id)
    })
  })

export const approvalCommandStatusSchema = z.enum([
  'queued',
  'processing',
  'succeeded',
  'manual_intervention',
])

export const approvalEffectStatusSchema = z.enum([
  'queued',
  'processing',
  'retryable_failed',
  'succeeded',
  'failed',
  'manual_intervention',
])

export const approvalEffectTypeSchema = z.enum([
  'stage_experiment_draft',
  'open_treatment_changeset',
  'open_code_changeset',
  'record_experiment_rejection',
  'record_proposal_rejection',
  'quarantine_feature_proposal',
])

export const approvalEffectSchema = z
  .object({
    effect_id: z.string().min(1),
    item_id: z.string().min(1).max(128),
    effect_type: approvalEffectTypeSchema,
    status: approvalEffectStatusSchema,
    attempt_count: z.number().int().nonnegative(),
    last_error: z.string().nullable(),
    result: z.record(z.unknown()).nullable(),
  })
  .strict()

// POST and status GET share one strict durable-command envelope. A successful
// POST means the decision and effect intents are committed, not that any
// Config or Codegen mutation has already completed.
export const approvalResponseSchema = z
  .object({
    command_id: z.string().min(1),
    run_id: z.string().min(1),
    actor_credential_id: z.string().regex(/^[A-Za-z0-9_-]{8,64}$/),
    actor_user_id: z.string().uuid().nullable(),
    gate_id: z.string().min(1),
    gate_agent: z.enum(['experiment_design', 'feature_proposal', 'code_implementation']),
    status: approvalCommandStatusSchema,
    approved_count: z.number().int().nonnegative(),
    rejected_count: z.number().int().nonnegative(),
    comment: z.string().nullable(),
    last_error: z.string().nullable(),
    created_at: z.string().min(1),
    updated_at: z.string().min(1),
    effects: z.array(approvalEffectSchema),
  })
  .strict()

// ---------- Run introspection (gaps G1+G2+G3, routers/runs.py) ----------

export const runsListResponseSchema = z
  .object({
    runs: z.array(runStatusSchema),
    count: z.number().int(),
  })
  .strict()

// Agent outputs are LLM-shaped and not canonicalized — arrays of unknown.
export const runResultsSchema = z
  .object({
    run_id: z.string(),
    insights: z.array(z.unknown()),
    experiment_designs: z.array(z.unknown()),
    personalizations: z.array(z.unknown()),
    feature_proposals: z.array(z.unknown()),
    changesets: z.array(z.unknown()),
    // Custom agents' outputs keyed by their produces. Optional: services
    // deploy independently, so an older backend may omit it.
    custom_outputs: z.record(z.array(z.unknown())).optional(),
  })
  .strict()

// ---------- Custom agents (routers/custom_agents.py) ----------

export const modelTierSchema = z.enum(['fast', 'reasoning'])

// One deterministic preset call: a catalog tool with params fixed at
// authoring time, executed on every run before reasoning. Params are
// validated against the tool's schema server-side.
export const presetToolCallSchema = z
  .object({
    tool: z.string().min(1),
    params: z.record(z.unknown()),
  })
  .strict()

// Create/update body (CustomAgentSpec). Server-side validate_definition owns
// the domain rules; these mirror the shape so bad payloads fail client-side.
// Custom agents are agentic: `tools` is the ALLOWED-tools selection (catalog
// names the model may call in its tool loop); an empty list allows no agentic
// tools. `max_tool_steps` bounds the loop's tool rounds. `preset_tools`
// are the deterministic calls run verbatim before the loop.
export const customAgentSpecSchema = z
  .object({
    slug: z
      .string()
      .regex(/^[a-z][a-z0-9_]{2,63}$/, 'lowercase letters, digits, underscores; 3-64 chars'),
    display_name: z.string().min(1).max(120),
    description: z.string().max(500),
    system_prompt: z
      .string()
      .min(1)
      .max(20_000)
      .refine((value) => value.trim().length > 0, 'Prompt must contain non-whitespace characters.'),
    user_prompt_template: z
      .string()
      .min(1)
      .max(20_000)
      .refine((value) => value.trim().length > 0, 'Prompt must contain non-whitespace characters.'),
    model_tier: modelTierSchema,
    tools: z.array(z.string().min(1)),
    preset_tools: z.array(presetToolCallSchema).max(10),
    requires: z.array(z.string()).max(5),
    produces: z
      .string()
      .regex(/^[a-z][a-z0-9_]{2,63}$/, 'lowercase letters, digits, underscores; 3-64 chars'),
    memory_query: z.string().max(500).nullable(),
    memory_top_k: z.number().int().min(1).max(20),
    pipeline_order: z.number().int().min(0).max(1000),
    max_tool_steps: z.number().int().min(1).max(16),
  })
  .strict()

export const customAgentSchema = customAgentSpecSchema
  .extend({
    agent_id: z.string(),
    project_id: z.string(),
    status: z.string(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const customAgentListSchema = z.array(customAgentSchema)

export const agentDefinitionSchema = z
  .object({
    name: z.string().min(1),
    display_name: z.string().min(1),
    description: z.string().min(1),
    order: z.number().int().nonnegative(),
    produces: z.string().min(1),
    requires: z.array(z.string().min(1)),
    model_tier: modelTierSchema,
    is_custom: z.boolean(),
    agent_id: z.string().min(1).nullable().optional(),
  })
  .strict()

export const toolCatalogEntrySchema = z
  .object({
    name: z.string(),
    description: z.string(),
    params_schema: z.record(z.unknown()),
  })
  .strict()

export const agentDefinitionsResponseSchema = z
  .object({
    agents: z.array(agentDefinitionSchema),
    tool_catalog: z.array(toolCatalogEntrySchema),
  })
  .strict()
  .superRefine((value, context) => {
    const names = new Set<string>()
    value.agents.forEach((agent, index) => {
      if (names.has(agent.name)) {
        context.addIssue({
          code: z.ZodIssueCode.custom,
          path: ['agents', index, 'name'],
          message: `duplicate agent definition: ${agent.name}`,
        })
      }
      names.add(agent.name)
    })
  })

export const testRunRequestSchema = z
  .object({
    project_id: z.string().min(1),
    time_range_days: z.number().int().min(1).max(90),
    definition: customAgentSpecSchema,
  })
  .strict()

export const testRunResponseSchema = z
  .object({
    prompt: z.string(),
    raw_response: z.string(),
    parsed_output: z.unknown(),
    preset_results: z.array(z.record(z.unknown())),
    tool_results: z.array(z.record(z.unknown())),
    timings_ms: z.record(z.number()),
  })
  .strict()

export const runAuditEntrySchema = z
  .object({
    id: z.number().int(),
    run_id: z.string(),
    action_type: z.string(),
    config: z.record(z.unknown()),
    safety_result: z.record(z.unknown()),
    approval_status: z.string().nullable(),
    created_at: z.string(),
  })
  .strict()

export const runAuditResponseSchema = z
  .object({
    run_id: z.string(),
    audit: z.array(runAuditEntrySchema),
    count: z.number().int(),
  })
  .strict()
