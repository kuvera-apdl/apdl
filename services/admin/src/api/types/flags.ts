// Canonical TypeScript types, inferred from the zod mirrors so the contract is
// encoded exactly once on the client.
import type { z } from 'zod'

import type {
  clientFlagConfigSchema,
  conditionOperatorSchema,
  evaluationModeSchema,
  experimentUpdatePayloadSchema,
  fallthroughConfigSchema,
  flagAuditActionSchema,
  flagAuditEntrySchema,
  flagAuditResponseSchema,
  flagCollectionSchema,
  flagConfigSchema,
  flagsListResponseSchema,
  flagStateSchema,
  flagUpdatePayloadSchema,
  gateConditionSchema,
  gateRuleSchema,
  guardrailConfigSchema,
  guardrailMetricSchema,
  guardrailThresholdSchema,
  rolloutConfigSchema,
  staleFlagSchema,
  staleFlagsResponseSchema,
  staleReasonSchema,
  variantConfigSchema,
} from '../schemas/flags'

export type ConditionOperator = z.infer<typeof conditionOperatorSchema>
export type GuardrailMetric = z.infer<typeof guardrailMetricSchema>
export type GuardrailThreshold = z.infer<typeof guardrailThresholdSchema>
export type EvaluationMode = z.infer<typeof evaluationModeSchema>
export type FlagState = z.infer<typeof flagStateSchema>
export type GateCondition = z.infer<typeof gateConditionSchema>
export type RolloutConfig = z.infer<typeof rolloutConfigSchema>
export type VariantConfig = z.infer<typeof variantConfigSchema>
export type GateRule = z.infer<typeof gateRuleSchema>
export type FallthroughConfig = z.infer<typeof fallthroughConfigSchema>
export type GuardrailConfig = z.infer<typeof guardrailConfigSchema>
export type FlagConfig = z.infer<typeof flagConfigSchema>
export type StaleReason = z.infer<typeof staleReasonSchema>
export type StaleFlag = z.infer<typeof staleFlagSchema>
export type FlagAuditAction = z.infer<typeof flagAuditActionSchema>
export type FlagAuditEntry = z.infer<typeof flagAuditEntrySchema>
export type FlagsListResponse = z.infer<typeof flagsListResponseSchema>
export type StaleFlagsResponse = z.infer<typeof staleFlagsResponseSchema>
export type FlagAuditResponse = z.infer<typeof flagAuditResponseSchema>
export type ClientFlagConfig = z.infer<typeof clientFlagConfigSchema>
export type FlagCollection = z.infer<typeof flagCollectionSchema>
export type FlagUpdatePayload = z.infer<typeof flagUpdatePayloadSchema>
export type ExperimentUpdatePayload = z.infer<typeof experimentUpdatePayloadSchema>
