import { z } from 'zod'

import { timeIntervalSchema } from '@/api/schemas/query'

import { dateRangeSchema, selectorFormValuesSchema } from './selectorModel'
export const eventsViewSchema = z
  .object({
    range: dateRangeSchema,
    countSelectors: z.array(selectorFormValuesSchema).min(1).max(20),
    tsSelector: selectorFormValuesSchema,
    interval: timeIntervalSchema,
    bdSelector: selectorFormValuesSchema,
    property: z.string(),
    limit: z.number().int().min(1).max(100),
  })
  .strict()

export type EventsView = z.infer<typeof eventsViewSchema>

export const funnelViewSchema = z
  .object({
    range: dateRangeSchema,
    steps: z.array(selectorFormValuesSchema).min(2).max(20),
    windowDays: z.number().int().min(1).max(90),
  })
  .strict()

export type FunnelView = z.infer<typeof funnelViewSchema>

export const retentionViewSchema = z
  .object({
    range: dateRangeSchema,
    cohortSelector: selectorFormValuesSchema,
    returnSelector: selectorFormValuesSchema,
    period: z.enum(['day', 'week']),
  })
  .strict()

export type RetentionView = z.infer<typeof retentionViewSchema>

export const cohortsViewSchema = z
  .object({
    range: dateRangeSchema,
    cohortProperty: z.string(),
    metricSelector: selectorFormValuesSchema,
  })
  .strict()

export type CohortsView = z.infer<typeof cohortsViewSchema>
