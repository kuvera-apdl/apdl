import type { z } from 'zod'

import type {
  experimentAnalysisDecisionSnapshotSchema,
  experimentAnalysisNonFinalSchema,
  experimentArmResultSchema,
  experimentComparisonSchema,
  experimentCreateResponseSchema,
  experimentCreateSchema,
  experimentDeleteResponseSchema,
  experimentEntrySchema,
  experimentMetricSchema,
  experimentStatisticalPlanSchema,
  experimentResultSchema,
  experimentsListResponseSchema,
  experimentStatusSchema,
  experimentUpdateResponseSchema,
  experimentUpdateSchema,
  experimentVariantSchema,
} from '../schemas/experiments'

export type ExperimentEntry = z.infer<typeof experimentEntrySchema>
export type ExperimentsListResponse = z.infer<typeof experimentsListResponseSchema>
export type ExperimentCreate = z.infer<typeof experimentCreateSchema>
export type ExperimentUpdate = z.infer<typeof experimentUpdateSchema>
export type ExperimentStatus = z.infer<typeof experimentStatusSchema>
export type ExperimentVariant = z.infer<typeof experimentVariantSchema>
export type ExperimentMetric = z.infer<typeof experimentMetricSchema>
export type ExperimentStatisticalPlan = z.infer<typeof experimentStatisticalPlanSchema>
export type ExperimentCreateResponse = z.infer<typeof experimentCreateResponseSchema>
export type ExperimentUpdateResponse = z.infer<typeof experimentUpdateResponseSchema>
export type ExperimentDeleteResponse = z.infer<typeof experimentDeleteResponseSchema>
export type ExperimentArmResult = z.infer<typeof experimentArmResultSchema>
export type ExperimentComparison = z.infer<typeof experimentComparisonSchema>
export type ExperimentAnalysisDecisionSnapshot = z.infer<
  typeof experimentAnalysisDecisionSnapshotSchema
>
export type ExperimentAnalysisNonFinal = z.infer<typeof experimentAnalysisNonFinalSchema>
export type ExperimentResult = z.infer<typeof experimentResultSchema>
