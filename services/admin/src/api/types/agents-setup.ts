import type { z } from 'zod'

import type {
  agentsModelSelectionSchema,
  agentsSetupAssignmentSchema,
  agentsSetupBlockerSchema,
  agentsSetupConnectionSchema,
  agentsSetupResponseSchema,
  deactivateAgentsSetupRequestSchema,
  llmConnectionDetailSchema,
  llmConnectionListSchema,
  llmConnectionSummarySchema,
  llmModelInventorySchema,
  llmProviderModelSchema,
  llmProviderSchema,
  providerDiscoveryErrorCodeSchema,
  putAgentsSetupRequestSchema,
  putLlmConnectionRequestSchema,
  refreshLlmConnectionRequestSchema,
  revokeLlmConnectionRequestSchema,
} from '@/api/schemas/agents-setup'

export type LlmProvider = z.infer<typeof llmProviderSchema>
export type LlmProviderModel = z.infer<typeof llmProviderModelSchema>
export type LlmConnectionSummary = z.infer<typeof llmConnectionSummarySchema>
export type LlmConnectionDetail = z.infer<typeof llmConnectionDetailSchema>
export type LlmConnectionList = z.infer<typeof llmConnectionListSchema>
export type LlmModelInventory = z.infer<typeof llmModelInventorySchema>
export type PutLlmConnectionRequest = z.infer<typeof putLlmConnectionRequestSchema>
export type RefreshLlmConnectionRequest = z.infer<typeof refreshLlmConnectionRequestSchema>
export type RevokeLlmConnectionRequest = z.infer<typeof revokeLlmConnectionRequestSchema>
export type ProviderDiscoveryErrorCode = z.infer<typeof providerDiscoveryErrorCodeSchema>
export type AgentsSetupBlocker = z.infer<typeof agentsSetupBlockerSchema>
export type AgentsModelSelection = z.infer<typeof agentsModelSelectionSchema>
export type AgentsSetupAssignment = z.infer<typeof agentsSetupAssignmentSchema>
export type AgentsSetupConnection = z.infer<typeof agentsSetupConnectionSchema>
export type AgentsSetup = z.infer<typeof agentsSetupResponseSchema>
export type PutAgentsSetupRequest = z.infer<typeof putAgentsSetupRequestSchema>
export type DeactivateAgentsSetupRequest = z.infer<
  typeof deactivateAgentsSetupRequestSchema
>
