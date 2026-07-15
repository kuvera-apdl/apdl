import type { z } from 'zod'

import type {
  agentDefinitionSchema,
  agentDefinitionsResponseSchema,
  analysisTypeSchema,
  approvalRequestSchema,
  approvalResponseSchema,
  customAgentSchema,
  customAgentSpecSchema,
  runAuditEntrySchema,
  runAuditResponseSchema,
  runResultsSchema,
  runsListResponseSchema,
  runStatusSchema,
  testRunRequestSchema,
  testRunResponseSchema,
  toolCatalogEntrySchema,
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
export type RunsListResponse = z.infer<typeof runsListResponseSchema>
export type RunResults = z.infer<typeof runResultsSchema>
export type RunAuditEntry = z.infer<typeof runAuditEntrySchema>
export type RunAuditResponse = z.infer<typeof runAuditResponseSchema>
export type CustomAgentSpec = z.infer<typeof customAgentSpecSchema>
export type CustomAgent = z.infer<typeof customAgentSchema>
export type AgentDefinition = z.infer<typeof agentDefinitionSchema>
export type ToolCatalogEntry = z.infer<typeof toolCatalogEntrySchema>
export type AgentDefinitionsResponse = z.infer<typeof agentDefinitionsResponseSchema>
export type TestRunRequest = z.infer<typeof testRunRequestSchema>
export type TestRunResponse = z.infer<typeof testRunResponseSchema>
