// Zod mirrors of services/query/app/models/schemas.py. The query service uses
// a DIFFERENT filter-operator vocabulary from flag rule conditions (AD-6) —
// the two are distinct types on purpose and must never be cross-assigned.
// Response row shapes mirror the exact SQL aliases in
// services/query/app/clickhouse/queries.py.
import { z } from 'zod'

export const eventFilterOperatorSchema = z.enum([
  'eq',
  'neq',
  'in',
  'not_in',
  'exists',
  'not_exists',
  'contains',
  'gt',
  'gte',
  'lt',
  'lte',
])

export const PROPERTY_NAME_PATTERN = /^[A-Za-z0-9_$][A-Za-z0-9_.$:-]{0,127}$/

const propertyNameSchema = z
  .string()
  .regex(
    PROPERTY_NAME_PATTERN,
    'Letters, numbers, _, -, :, ., $ only; must start with a letter, number, _ or $',
  )

const filterScalarSchema = z.union([z.string(), z.number(), z.boolean()])

// Like flag conditions, exists/not_exists must OMIT the value key on the wire
// (Pydantic model_fields_set); JSON.stringify drops undefined.
export const eventPropertyFilterSchema = z
  .object({
    property: propertyNameSchema,
    operator: eventFilterOperatorSchema,
    value: z.unknown().optional(),
  })
  .strict()
  .superRefine((filter, ctx) => {
    const issue = (message: string) =>
      ctx.addIssue({ code: z.ZodIssueCode.custom, path: ['value'], message })
    if (filter.operator === 'exists' || filter.operator === 'not_exists') {
      if (filter.value !== undefined) issue(`${filter.operator} does not accept a value`)
      return
    }
    if (filter.value === undefined || filter.value === null) {
      issue(`${filter.operator} requires a value`)
      return
    }
    if (filter.operator === 'in' || filter.operator === 'not_in') {
      const parsed = z.array(filterScalarSchema).min(1).safeParse(filter.value)
      if (!parsed.success) issue(`${filter.operator} requires a non-empty list of scalar values`)
      return
    }
    if (filter.operator === 'contains') {
      if (typeof filter.value !== 'string' || filter.value === '') {
        issue('contains requires a non-empty string value')
      }
      return
    }
    if (['gt', 'gte', 'lt', 'lte'].includes(filter.operator)) {
      if (typeof filter.value !== 'number' || !Number.isFinite(filter.value)) {
        issue(`${filter.operator} requires a numeric value`)
      }
      return
    }
    if (!filterScalarSchema.safeParse(filter.value).success) {
      issue(`${filter.operator} requires a scalar value`)
    }
  })

export const eventSelectorSchema = z
  .object({
    event_name: z.string().min(1).max(256),
    filters: z.array(eventPropertyFilterSchema).max(25),
  })
  .strict()

const dateSchema = z.string().regex(/^\d{4}-\d{2}-\d{2}$/, 'YYYY-MM-DD')

function dateOrder(request: { start_date: string; end_date: string }, ctx: z.RefinementCtx): void {
  if (request.end_date < request.start_date) {
    ctx.addIssue({
      code: z.ZodIssueCode.custom,
      path: ['end_date'],
      message: 'end_date must be on or after start_date',
    })
  }
}

export const timeIntervalSchema = z.enum(['1 HOUR', '1 DAY', '1 WEEK', '1 MONTH'])

export const eventCountRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    selectors: z.array(eventSelectorSchema).min(1).max(20),
  })
  .strict()
  .superRefine(dateOrder)

export const timeseriesRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    selector: eventSelectorSchema,
    interval: timeIntervalSchema,
  })
  .strict()
  .superRefine(dateOrder)

export const breakdownRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    selector: eventSelectorSchema,
    property: propertyNameSchema,
    limit: z.number().int().min(1).max(100),
  })
  .strict()
  .superRefine(dateOrder)

export const funnelRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    steps: z.array(eventSelectorSchema).min(2).max(20),
    window_days: z.number().int().min(1).max(90),
  })
  .strict()
  .superRefine(dateOrder)

export const retentionRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    cohort_selector: eventSelectorSchema,
    return_selector: eventSelectorSchema,
    cohort_mode: z.literal('first_match_in_window'),
    period: z.enum(['day', 'week']),
  })
  .strict()
  .superRefine(dateOrder)

export const cohortRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    cohort_property: propertyNameSchema,
    metric_selector: eventSelectorSchema,
  })
  .strict()
  .superRefine(dateOrder)

// ---------- Responses ----------

const countRowSchema = z
  .object({
    selector: z.string(),
    event_name: z.string(),
    event_count: z.number(),
    unique_users: z.number(),
  })
  .strict()

export const eventCountResponseSchema = z
  .object({
    results: z.array(countRowSchema),
    total_events: z.number(),
    total_users: z.number(),
  })
  .strict()

const timeseriesBucketSchema = z
  .object({
    selector: z.string(),
    bucket: z.string(),
    event_count: z.number(),
    unique_users: z.number(),
  })
  .strict()

export const timeseriesResponseSchema = z
  .object({
    selector: z.string(),
    buckets: z.array(timeseriesBucketSchema),
  })
  .strict()

const breakdownRowSchema = z
  .object({
    selector: z.string().min(1),
    property_type: z.enum(['string', 'integer', 'float', 'boolean']),
    property_value: z.string(),
    event_count: z.number().int().nonnegative(),
    unique_users: z.number().int().nonnegative(),
  })
  .strict()

export const breakdownResponseSchema = z
  .object({
    selector: z.string(),
    property: z.string(),
    results: z.array(breakdownRowSchema),
  })
  .strict()

export const funnelStepSchema = z
  .object({
    step: z.number().int(),
    event_name: z.string(),
    selector: z.string(),
    count: z.number(),
    // Percentages 0–100, rounded to 2 decimals server-side.
    conversion_rate: z.number(),
    overall_rate: z.number(),
  })
  .strict()

export const funnelResponseSchema = z
  .object({
    steps: z.array(funnelStepSchema),
    overall_conversion: z.number(),
  })
  .strict()

export const retentionCohortSchema = z
  .object({
    cohort_date: z.string(),
    size: z.number().int(),
    retention: z.array(z.number()),
  })
  .strict()

export const retentionResponseSchema = z
  .object({
    cohort_mode: z.literal('first_match_in_window'),
    cohort_selector: z.string(),
    return_selector: z.string(),
    cohorts: z.array(retentionCohortSchema),
  })
  .strict()

const cohortPointSchema = z
  .object({
    day: z.string().nullable(),
    event_count: z.number(),
    unique_users: z.number(),
  })
  .strict()

const cohortEntrySchema = z
  .object({
    cohort_value: z.string(),
    total_events: z.number(),
    total_users: z.number(),
    timeseries: z.array(cohortPointSchema),
  })
  .strict()

export const cohortResponseSchema = z
  .object({
    metric_selector: z.string(),
    cohort_property: z.string(),
    cohorts: z.array(cohortEntrySchema),
  })
  .strict()

// ---------- Event catalog (discovery, gap G4) ----------

export const eventCatalogRequestSchema = z
  .object({
    project_id: z.string().min(1),
    start_date: dateSchema,
    end_date: dateSchema,
    limit: z.number().int().min(1).max(1000),
  })
  .strict()
  .superRefine(dateOrder)

const eventCatalogEntrySchema = z
  .object({
    event_name: z.string(),
    event_count: z.number(),
    unique_users: z.number(),
  })
  .strict()

export const eventCatalogResponseSchema = z
  .object({
    events: z.array(eventCatalogEntrySchema),
  })
  .strict()
