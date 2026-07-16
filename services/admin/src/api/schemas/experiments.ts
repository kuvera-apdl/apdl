// Experiment mirrors of services/config/app/models/schemas.py (Strict Schema
// Rule). Canonicalized in gap G5: the experiment owns a backing flag, so the
// record reuses the flag's variant/rule contracts rather than loose JSON.
import { z } from 'zod'

import { gateRuleSchema } from './flags'
import { MAX_IDENTIFIER_LENGTH } from '@/core/evaluator/targetingContract'

const MAX_EXPERIMENT_VARIANTS = 10
const PATH_KEY_PATTERN = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/

export const experimentPathKeySchema = z
  .string()
  .regex(PATH_KEY_PATTERN, 'Use 1–128 letters, numbers, dots, underscores, or hyphens')

export const experimentStatusSchema = z.enum([
  'draft',
  'scheduled',
  'running',
  'completed',
  'stopped',
])
export const experimentCreateStatusSchema = z.enum(['draft', 'scheduled', 'running'])

const awareDateTimeSchema = z.string().datetime({ offset: true })

// Variants carry an optional display description on top of the canonical
// {key, weight} that the backing flag projects down to.
export const experimentVariantSchema = z
  .object({
    key: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    weight: z.number().int().positive(),
    description: z.string().optional(),
  })
  .strict()

export const experimentMetricSchema = z
  .object({
    event: z.string().min(1),
    type: z.literal('conversion'),
    direction: z.string(),
  })
  .strict()

// GET /v1/admin/experiments rows (routers/admin.py list_experiments).
export const experimentEntrySchema = z
  .object({
    key: experimentPathKeySchema,
    flag_key: experimentPathKeySchema,
    status: experimentStatusSchema,
    description: z.string(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    traffic_percentage: z.number(),
    variants: z.array(experimentVariantSchema).min(2).max(MAX_EXPERIMENT_VARIANTS),
    targeting_rules: z.array(gateRuleSchema),
    primary_metric: experimentMetricSchema.nullable(),
    start_date: awareDateTimeSchema.nullable(),
    end_date: awareDateTimeSchema.nullable(),
    version: z.number().int().min(1),
    created_at: awareDateTimeSchema,
    updated_at: awareDateTimeSchema,
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
    key: experimentPathKeySchema,
    flag_key: experimentPathKeySchema.optional(),
    status: experimentCreateStatusSchema,
    description: z.string(),
    traffic_percentage: z.number().min(0).max(100),
    start_date: awareDateTimeSchema.nullable().optional(),
    end_date: awareDateTimeSchema.nullable().optional(),
    variants: z.array(experimentVariantSchema).min(2).max(MAX_EXPERIMENT_VARIANTS),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH),
    primary_metric: experimentMetricSchema.optional(),
    targeting_rules: z.array(gateRuleSchema),
  })
  .strict()

export const experimentUpdateSchema = z
  .object({
    version: z.number().int().min(1),
    status: experimentStatusSchema.optional(),
    description: z.string().optional(),
    traffic_percentage: z.number().min(0).max(100).optional(),
    start_date: awareDateTimeSchema.nullable().optional(),
    end_date: awareDateTimeSchema.nullable().optional(),
    variants: z.array(experimentVariantSchema).min(2).max(MAX_EXPERIMENT_VARIANTS).optional(),
    default_variant: z.string().min(1).max(MAX_IDENTIFIER_LENGTH).optional(),
    primary_metric: experimentMetricSchema.nullable().optional(),
    targeting_rules: z.array(gateRuleSchema).optional(),
  })
  .strict()

export const experimentCreateResponseSchema = z
  .object({
    created: z.boolean(),
    key: experimentPathKeySchema,
    flag_key: experimentPathKeySchema,
    version: z.number().int().min(1),
  })
  .strict()
export const experimentUpdateResponseSchema = z
  .object({
    updated: z.boolean(),
    key: experimentPathKeySchema,
    flag_key: experimentPathKeySchema,
    version: z.number().int().min(1),
  })
  .strict()
export const experimentDeleteResponseSchema = z
  .object({
    deleted: z.boolean(),
    key: experimentPathKeySchema,
    flag_key: experimentPathKeySchema,
    version: z.number().int().min(1),
  })
  .strict()

// GET /v1/query/experiment/{key}. Query resolves every analysis input from
// authoritative Config metadata; callers provide only the project scope.
const finiteNumberSchema = z.number().finite()
const probabilitySchema = finiteNumberSchema.min(0).max(1)

export const experimentArmResultSchema = z
  .object({
    variant: z.string().min(1),
    sample_size: z.number().int().min(0),
    conversions: z.number().int().min(0),
    conversion_rate: probabilitySchema,
  })
  .strict()

export const experimentComparisonSchema = z
  .object({
    control_variant: z.string().min(1),
    treatment_variant: z.string().min(1),
    control_rate: probabilitySchema,
    treatment_rate: probabilitySchema,
    rate_difference: finiteNumberSchema.min(-1).max(1),
    confidence_interval: z.tuple([finiteNumberSchema, finiteNumberSchema]),
    raw_p_value: probabilitySchema,
    adjusted_p_value: probabilitySchema,
    is_significant: z.boolean(),
  })
  .strict()

const experimentAnalysisBaseShape = {
  experiment_key: experimentPathKeySchema,
  flag_key: experimentPathKeySchema,
  experiment_status: z.enum(['scheduled', 'running', 'completed', 'stopped']),
  control_variant: z.string().min(1),
  metric_event: z.string().min(1),
  start_date: awareDateTimeSchema,
  end_date: awareDateTimeSchema,
  config_version: z.number().int().min(1),
  arms: z.array(experimentArmResultSchema),
  crossover_actors: z.number().int().min(0),
  unknown_variant_actors: z.number().int().min(0),
}

export const experimentAnalysisReadySchema = z
  .object({
    analysis_status: z.literal('ready'),
    ...experimentAnalysisBaseShape,
    significance_level: finiteNumberSchema.gt(0).lt(1),
    correction: z.literal('bonferroni'),
    comparisons: z.array(experimentComparisonSchema),
  })
  .strict()

export const experimentAnalysisInsufficientSchema = z
  .object({
    analysis_status: z.literal('insufficient_data'),
    ...experimentAnalysisBaseShape,
    reason: z.enum([
      'experiment_not_started',
      'no_exposures',
      'underpowered_arms',
      'non_finite_statistics',
    ]),
    minimum_sample_size_per_arm: z.number().int().min(2),
    underpowered_variants: z.array(z.string().min(1)),
  })
  .strict()

export const experimentResultSchema = z.discriminatedUnion('analysis_status', [
  experimentAnalysisReadySchema,
  experimentAnalysisInsufficientSchema,
])
