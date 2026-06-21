// Experiment mirrors of services/config/app/models/schemas.py (Strict Schema
// Rule). Canonicalized in gap G5: the experiment owns a backing flag, so the
// record reuses the flag's variant/rule contracts rather than loose JSON.
import { z } from 'zod'

import { gateRuleSchema, variantConfigSchema } from './flags'

export const experimentStatusSchema = z.enum(['draft', 'running', 'completed', 'stopped'])

// Variants carry an optional display description on top of the canonical
// {key, weight} that the backing flag projects down to.
export const experimentVariantSchema = variantConfigSchema
  .extend({ description: z.string().optional() })
  .strict()

export const experimentMetricSchema = z
  .object({
    event: z.string().min(1),
    type: z.string(),
    direction: z.string(),
  })
  .strict()

// GET /v1/admin/experiments rows (routers/admin.py list_experiments).
export const experimentEntrySchema = z
  .object({
    key: z.string(),
    flag_key: z.string(),
    status: experimentStatusSchema,
    description: z.string(),
    default_variant: z.string(),
    traffic_percentage: z.number(),
    variants: z.array(experimentVariantSchema),
    targeting_rules: z.array(gateRuleSchema),
    primary_metric: experimentMetricSchema.nullable(),
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
    flag_key: z.string().min(1).optional(),
    status: experimentStatusSchema,
    description: z.string(),
    traffic_percentage: z.number().min(0).max(100),
    start_date: z.string(),
    end_date: z.string(),
    variants: z.array(experimentVariantSchema),
    default_variant: z.string().min(1).optional(),
    primary_metric: experimentMetricSchema.optional(),
    targeting_rules: z.array(gateRuleSchema),
  })
  .strict()

export const experimentUpdateSchema = z
  .object({
    status: experimentStatusSchema.optional(),
    description: z.string().optional(),
    traffic_percentage: z.number().min(0).max(100).optional(),
    start_date: z.string().optional(),
    end_date: z.string().optional(),
    variants: z.array(experimentVariantSchema).optional(),
    default_variant: z.string().min(1).optional(),
    primary_metric: experimentMetricSchema.optional(),
    targeting_rules: z.array(gateRuleSchema).optional(),
  })
  .strict()

export const experimentCreateResponseSchema = z
  .object({ created: z.boolean(), key: z.string(), flag_key: z.string() })
  .strict()
export const experimentUpdateResponseSchema = z
  .object({ updated: z.boolean(), key: z.string(), flag_key: z.string() })
  .strict()
export const experimentDeleteResponseSchema = z
  .object({ deleted: z.boolean(), key: z.string(), flag_key: z.string() })
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
