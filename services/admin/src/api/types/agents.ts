import type { z } from 'zod'

import type {
  analysisTypeSchema,
  approvalRequestSchema,
  approvalResponseSchema,
  runStatusSchema,
  triggerRequestSchema,
  triggerResponseSchema,
  triggerTypeSchema,
} from '../schemas/agents'

export type AnalysisType = z.infer<typeof analysisTypeSchema>
export type TriggerType = z.infer<typeof triggerTypeSchema>
export type TriggerRequest = z.infer<typeof triggerRequestSchema>
export type TriggerResponse = z.infer<typeof triggerResponseSchema>
export type RunStatus = z.infer<typeof runStatusSchema>
export type ApprovalRequest = z.infer<typeof approvalRequestSchema>
export type ApprovalResponse = z.infer<typeof approvalResponseSchema>
