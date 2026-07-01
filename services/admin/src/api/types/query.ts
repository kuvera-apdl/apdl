// Query-service types, inferred from the zod mirrors. Note: the filter
// operator vocabulary here is deliberately distinct from flag rule
// ConditionOperator (AD-6) — the types must never be cross-assigned.
import type { z } from 'zod'

import type {
  breakdownRequestSchema,
  breakdownResponseSchema,
  cohortRequestSchema,
  cohortResponseSchema,
  eventCatalogRequestSchema,
  eventCatalogResponseSchema,
  eventCountRequestSchema,
  eventCountResponseSchema,
  eventFilterOperatorSchema,
  eventPropertyFilterSchema,
  eventSelectorSchema,
  funnelRequestSchema,
  funnelResponseSchema,
  funnelStepSchema,
  retentionCohortSchema,
  retentionRequestSchema,
  retentionResponseSchema,
  timeIntervalSchema,
  timeseriesRequestSchema,
  timeseriesResponseSchema,
} from '../schemas/query'

export type EventFilterOperator = z.infer<typeof eventFilterOperatorSchema>
export type EventPropertyFilter = z.infer<typeof eventPropertyFilterSchema>
export type EventSelector = z.infer<typeof eventSelectorSchema>
export type TimeInterval = z.infer<typeof timeIntervalSchema>
export type EventCatalogRequest = z.infer<typeof eventCatalogRequestSchema>
export type EventCatalogResponse = z.infer<typeof eventCatalogResponseSchema>
export type EventCountRequest = z.infer<typeof eventCountRequestSchema>
export type EventCountResponse = z.infer<typeof eventCountResponseSchema>
export type TimeseriesRequest = z.infer<typeof timeseriesRequestSchema>
export type TimeseriesResponse = z.infer<typeof timeseriesResponseSchema>
export type BreakdownRequest = z.infer<typeof breakdownRequestSchema>
export type BreakdownResponse = z.infer<typeof breakdownResponseSchema>
export type FunnelRequest = z.infer<typeof funnelRequestSchema>
export type FunnelStep = z.infer<typeof funnelStepSchema>
export type FunnelResponse = z.infer<typeof funnelResponseSchema>
export type RetentionRequest = z.infer<typeof retentionRequestSchema>
export type RetentionCohort = z.infer<typeof retentionCohortSchema>
export type RetentionResponse = z.infer<typeof retentionResponseSchema>
export type CohortRequest = z.infer<typeof cohortRequestSchema>
export type CohortResponse = z.infer<typeof cohortResponseSchema>
