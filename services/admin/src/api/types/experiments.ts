import type { z } from 'zod'

import type {
  analysisMethodSchema,
  experimentCreateResponseSchema,
  experimentCreateSchema,
  experimentDeleteResponseSchema,
  experimentEntrySchema,
  experimentResultSchema,
  experimentsListResponseSchema,
  experimentUpdateResponseSchema,
  experimentUpdateSchema,
  variantResultSchema,
} from '../schemas/experiments'

export type ExperimentEntry = z.infer<typeof experimentEntrySchema>
export type ExperimentsListResponse = z.infer<typeof experimentsListResponseSchema>
export type ExperimentCreate = z.infer<typeof experimentCreateSchema>
export type ExperimentUpdate = z.infer<typeof experimentUpdateSchema>
export type ExperimentCreateResponse = z.infer<typeof experimentCreateResponseSchema>
export type ExperimentUpdateResponse = z.infer<typeof experimentUpdateResponseSchema>
export type ExperimentDeleteResponse = z.infer<typeof experimentDeleteResponseSchema>
export type AnalysisMethod = z.infer<typeof analysisMethodSchema>
export type VariantResult = z.infer<typeof variantResultSchema>
export type ExperimentResult = z.infer<typeof experimentResultSchema>
