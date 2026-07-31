import type { z } from 'zod'

import type {
  codegenLlmConnectionDetailSchema,
  codegenLlmConnectionListSchema,
  codegenLlmConnectionSummarySchema,
  codegenLlmModelInventorySchema,
  codegenLlmProviderModelSchema,
  codegenLlmProviderSchema,
  codegenLlmRoleSchema,
  putCodegenLlmConnectionRequestSchema,
  refreshCodegenLlmConnectionRequestSchema,
  revokeCodegenLlmConnectionRequestSchema,
} from '../schemas/codegen-llm-connections'

export type CodegenLlmProvider = z.infer<typeof codegenLlmProviderSchema>
export type CodegenLlmRole = z.infer<typeof codegenLlmRoleSchema>
export type CodegenLlmProviderModel = z.infer<typeof codegenLlmProviderModelSchema>
export type CodegenLlmConnectionSummary = z.infer<
  typeof codegenLlmConnectionSummarySchema
>
export type CodegenLlmConnectionDetail = z.infer<
  typeof codegenLlmConnectionDetailSchema
>
export type CodegenLlmConnectionList = z.infer<typeof codegenLlmConnectionListSchema>
export type CodegenLlmModelInventory = z.infer<typeof codegenLlmModelInventorySchema>
export type PutCodegenLlmConnectionRequest = z.infer<
  typeof putCodegenLlmConnectionRequestSchema
>
export type RefreshCodegenLlmConnectionRequest = z.infer<
  typeof refreshCodegenLlmConnectionRequestSchema
>
export type RevokeCodegenLlmConnectionRequest = z.infer<
  typeof revokeCodegenLlmConnectionRequestSchema
>
