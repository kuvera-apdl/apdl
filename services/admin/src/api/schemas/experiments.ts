// Experiment mirrors. The experiment record is deliberately loose,
// pre-canonicalization (plan §1.5 / D4): status is a free-form string and
// variants/targeting_rules are unvalidated JSON arrays — the console mirrors
// that looseness rather than inventing schema. Canonicalization is gap G5.
import { z } from 'zod'

// GET /v1/admin/experiments rows (routers/admin.py list_experiments):
// `variants` is present only when variants_json was a non-empty array.
export const experimentEntrySchema = z
  .object({
    key: z.string(),
    status: z.string(),
    description: z.string(),
    traffic_percentage: z.number(),
    variants: z.array(z.unknown()).optional(),
    start_date: z.string(),
    end_date: z.string(),
    created_at: z.string(),
    updated_at: z.string(),
  })
  .strict()

export const experimentsListResponseSchema = z
  .object({
    experiments: z.array(experimentEntrySchema),
    count: z.number().int(),
  })
  .strict()

export const experimentCreateSchema = z
  .object({
    key: z.string().min(1),
    status: z.string(),
    description: z.string(),
    traffic_percentage: z.number().min(0).max(100),
    start_date: z.string(),
    end_date: z.string(),
    variants: z.array(z.unknown()),
    targeting_rules: z.array(z.unknown()),
  })
  .strict()

export const experimentUpdateSchema = z
  .object({
    status: z.string().optional(),
    description: z.string().optional(),
    traffic_percentage: z.number().min(0).max(100).optional(),
    start_date: z.string().optional(),
    end_date: z.string().optional(),
    variants: z.array(z.unknown()).optional(),
    targeting_rules: z.array(z.unknown()).optional(),
  })
  .strict()

export const experimentCreateResponseSchema = z
  .object({ created: z.boolean(), key: z.string() })
  .strict()
export const experimentUpdateResponseSchema = z
  .object({ updated: z.boolean(), key: z.string() })
  .strict()
export const experimentDeleteResponseSchema = z
  .object({ deleted: z.boolean(), key: z.string() })
  .strict()

// GET /v1/query/experiment/{id} (query service ExperimentResult).
export const analysisMethodSchema = z.enum(['frequentist', 'bayesian', 'sequential'])

export const variantResultSchema = z
  .object({
    variant: z.string(),
    users: z.number().int(),
    mean: z.number(),
    stddev: z.number(),
    total: z.number(),
  })
  .strict()

export const experimentResultSchema = z
  .object({
    experiment_id: z.string(),
    flag_key: z.string(),
    metric: z.string(),
    method: z.string(),
    variants: z.array(variantResultSchema),
    effect_size: z.number().nullable(),
    confidence_interval: z.tuple([z.number(), z.number()]).nullable(),
    p_value: z.number().nullable(),
    is_significant: z.boolean(),
    recommendation: z.string(),
  })
  .strict()
