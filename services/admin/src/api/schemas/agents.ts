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
    run_id: z.string(),
    status: z.string(),
  })
  .strict()

// Run lifecycle vocabularies (supervisor.py); status/phase are plain strings
// in the server model, so the response schema stays string-typed and the UI
// maps known values.
export const KNOWN_RUN_STATUSES = [
  'started',
  'running',
  'waiting_approval',
  'approved',
  'rejected',
  'completed',
  'completed_with_errors',
  'failed',
] as const

export const TERMINAL_RUN_STATUSES = new Set<string>([
  'completed',
  'completed_with_errors',
  'failed',
  'rejected',
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
  })
  .strict()

export const itemDecisionSchema = z
  .object({
    item_id: z.string(),
    approved: z.boolean(),
  })
  .strict()

// Per-item batched decisions (one per gated item) OR a legacy whole-gate
// `approved`. The server requires exactly one; the console sends `decisions`.
export const approvalRequestSchema = z
  .object({
    decisions: z.array(itemDecisionSchema).optional(),
    approved: z.boolean().optional(),
    comment: z.string().optional(),
  })
  .strict()
  // The server requires exactly one of `decisions` / `approved` (400s otherwise).
  // Enforce it here so a future caller is caught client-side, not after a round
  // trip. Current callers always send exactly one.
  .refine((v) => (v.decisions !== undefined) !== (v.approved !== undefined), {
    message: 'Provide exactly one of `decisions` or `approved`.',
  })

// The count/array fields were added after the original { run_id, status,
// message } envelope. Services deploy independently, so the console may briefly
// run against a backend that still returns the old shape — keep these tolerant
// (optional + default) so a successful approval never trips schema_mismatch and
// surfaces a spurious "Decision failed" while the run was approved server-side.
export const approvalResponseSchema = z
  .object({
    run_id: z.string(),
    status: z.string(),
    approved_count: z.number().int().optional(),
    rejected_count: z.number().int().optional(),
    forked_runs: z.array(z.string()).optional(),
    opened_changesets: z.array(z.string()).optional(),
    message: z.string(),
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
export const parseAsSchema = z.enum(['object', 'list'])

// Create/update body (CustomAgentSpec). Server-side validate_definition owns
// the domain rules; these mirror the shape so bad payloads fail client-side.
// Custom agents are agentic: `tools` is the ALLOWED-tools selection (catalog
// names the model may call in its tool loop); an empty list allows the whole
// catalog. `max_tool_steps` bounds the loop's tool rounds.
export const customAgentSpecSchema = z
  .object({
    slug: z
      .string()
      .regex(/^[a-z][a-z0-9_]{2,63}$/, 'lowercase letters, digits, underscores; 3-64 chars'),
    display_name: z.string().min(1).max(120),
    description: z.string().max(500),
    system_prompt: z.string().min(1).max(20_000),
    user_prompt_template: z.string().min(1).max(20_000),
    model_tier: modelTierSchema,
    tools: z.array(z.string().min(1)),
    requires: z.array(z.string()).max(5),
    produces: z
      .string()
      .regex(/^[a-z][a-z0-9_]{2,63}$/, 'lowercase letters, digits, underscores; 3-64 chars'),
    parse_as: parseAsSchema,
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
    name: z.string(),
    display_name: z.string(),
    description: z.string(),
    order: z.number().int(),
    produces: z.string(),
    requires: z.array(z.string()),
    model_tier: z.string(),
    is_custom: z.boolean(),
    agent_id: z.string().nullable().optional(),
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
