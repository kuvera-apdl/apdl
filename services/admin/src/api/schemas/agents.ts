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
    analysis_types: z.array(analysisTypeSchema).min(1),
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

export const approvalRequestSchema = z
  .object({
    approved: z.boolean(),
    comment: z.string().optional(),
  })
  .strict()

export const approvalResponseSchema = z
  .object({
    run_id: z.string(),
    status: z.string(),
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
