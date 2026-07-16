// Canonical TypeScript types, inferred from the zod mirrors so the contract is
// encoded exactly once on the client.
import type { z } from 'zod'

import type {
  clientFlagConfigSchema,
  conditionOperatorSchema,
  evalContextSchema,
  evaluationModeSchema,
  experimentUpdatePayloadSchema,
  flagArchiveResponseSchema,
  flagCleanupResponseSchema,
  flagCleanupSchema,
  flagCreateResponseSchema,
  flagCreateSchema,
  flagDisableResponseSchema,
  flagDisableSchema,
  flagTransitionResponseSchema,
  flagTransitionSchema,
  flagUpdateResponseSchema,
  flagUpdateSchema,
  gateEvaluateRequestSchema,
  gateEvaluateResponseSchema,
  writableFlagStateSchema,
  fallthroughConfigSchema,
  flagAuditActionSchema,
  flagAuditEntrySchema,
  flagAuditOriginSchema,
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
export type FlagAuditOrigin = z.infer<typeof flagAuditOriginSchema>
export type FlagAuditEntry = z.infer<typeof flagAuditEntrySchema>
export type FlagsListResponse = z.infer<typeof flagsListResponseSchema>
export type StaleFlagsResponse = z.infer<typeof staleFlagsResponseSchema>
export type FlagAuditResponse = z.infer<typeof flagAuditResponseSchema>
export type ClientFlagConfig = z.infer<typeof clientFlagConfigSchema>
export type FlagCollection = z.infer<typeof flagCollectionSchema>
export type FlagUpdatePayload = z.infer<typeof flagUpdatePayloadSchema>
export type ExperimentUpdatePayload = z.infer<typeof experimentUpdatePayloadSchema>

export type WritableFlagState = z.infer<typeof writableFlagStateSchema>
export type FlagCreate = z.infer<typeof flagCreateSchema>
export type FlagUpdate = z.infer<typeof flagUpdateSchema>
export type FlagTransition = z.infer<typeof flagTransitionSchema>
export type FlagDisable = z.infer<typeof flagDisableSchema>
export type FlagCleanup = z.infer<typeof flagCleanupSchema>
export type FlagCreateResponse = z.infer<typeof flagCreateResponseSchema>
export type FlagUpdateResponse = z.infer<typeof flagUpdateResponseSchema>
export type FlagTransitionResponse = z.infer<typeof flagTransitionResponseSchema>
export type FlagDisableResponse = z.infer<typeof flagDisableResponseSchema>
export type FlagArchiveResponse = z.infer<typeof flagArchiveResponseSchema>
export type FlagCleanupResponse = z.infer<typeof flagCleanupResponseSchema>
export type EvalContext = z.infer<typeof evalContextSchema>
export type GateEvaluateRequest = z.infer<typeof gateEvaluateRequestSchema>
export type GateEvaluateResponse = z.infer<typeof gateEvaluateResponseSchema>
