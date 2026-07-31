import {
  agentsSetupResponseSchema,
  deactivateAgentsSetupRequestSchema,
  llmConnectionDetailSchema,
  llmConnectionListSchema,
  llmConnectionSummarySchema,
  llmModelInventorySchema,
  llmProviderSchema,
  providerDiscoveryErrorBodySchema,
  putAgentsSetupRequestSchema,
  putLlmConnectionRequestSchema,
  refreshLlmConnectionRequestSchema,
  revokeLlmConnectionRequestSchema,
} from '@/api/schemas/agents-setup'
import type {
  AgentsModelSelection,
  AgentsSetup,
  LlmConnectionDetail,
  LlmConnectionList,
  LlmConnectionSummary,
  LlmModelInventory,
  LlmProvider,
  ProviderDiscoveryErrorCode,
} from '@/api/types/agents-setup'
import { ApiError, request, type ServiceConnection } from '@/api/http'

interface RequestOptions {
  signal?: AbortSignal
}

function assertProject(projectId: string, responseProjectId: string): void {
  if (responseProjectId !== projectId) {
    throw new ApiError(
      200,
      'project_authority_mismatch',
      'Agents setup response crossed project authority',
    )
  }
}

function assertProvider(provider: LlmProvider, responseProvider: LlmProvider): void {
  if (responseProvider !== provider) {
    throw new ApiError(
      200,
      'provider_authority_mismatch',
      'Agents setup response crossed provider authority',
    )
  }
}

export function listProjectLlmConnections(
  conn: ServiceConnection,
  projectId: string,
  options: RequestOptions = {},
): Promise<LlmConnectionList> {
  return request(conn, '/v1/agents/llm-connections', {
    query: { project_id: projectId },
    schema: llmConnectionListSchema,
    signal: options.signal,
  }).then((response) => {
    assertProject(projectId, response.project_id)
    return response
  })
}

export function getProjectLlmModels(
  conn: ServiceConnection,
  projectId: string,
  provider: LlmProvider,
  options: RequestOptions = {},
): Promise<LlmModelInventory> {
  const canonicalProvider = llmProviderSchema.parse(provider)
  return request(
    conn,
    `/v1/agents/llm-connections/${encodeURIComponent(canonicalProvider)}/models`,
    {
      query: { project_id: projectId },
      schema: llmModelInventorySchema,
      signal: options.signal,
    },
  ).then((response) => {
    assertProject(projectId, response.project_id)
    assertProvider(canonicalProvider, response.provider)
    return response
  })
}

/**
 * Provider keys deliberately bypass TanStack mutations: mutation variables are
 * inspectable and retained after settlement. The caller owns immediate input
 * cleanup in a finally block.
 */
export function putProjectLlmConnection(
  conn: ServiceConnection,
  projectId: string,
  provider: LlmProvider,
  apiKey: string,
  version: number,
  options: RequestOptions = {},
): Promise<LlmConnectionDetail> {
  const canonicalProvider = llmProviderSchema.parse(provider)
  const body = putLlmConnectionRequestSchema.parse({
    project_id: projectId,
    api_key: apiKey,
    version,
  })
  return request(
    conn,
    `/v1/agents/llm-connections/${encodeURIComponent(canonicalProvider)}`,
    {
      method: 'PUT',
      body,
      schema: llmConnectionDetailSchema,
      signal: options.signal,
      // A provider may return 401 for the submitted provider key; that does
      // not mean the Admin session itself is unauthorized.
      redirectOnUnauthorized: false,
    },
  ).then((response) => {
    assertProject(projectId, response.project_id)
    assertProvider(canonicalProvider, response.provider)
    return response
  })
}

export function refreshProjectLlmModels(
  conn: ServiceConnection,
  projectId: string,
  provider: LlmProvider,
  version: number,
  options: RequestOptions = {},
): Promise<LlmConnectionDetail> {
  const canonicalProvider = llmProviderSchema.parse(provider)
  const body = refreshLlmConnectionRequestSchema.parse({
    project_id: projectId,
    version,
  })
  return request(
    conn,
    `/v1/agents/llm-connections/${encodeURIComponent(canonicalProvider)}/refresh-models`,
    {
      method: 'POST',
      body,
      schema: llmConnectionDetailSchema,
      signal: options.signal,
      redirectOnUnauthorized: false,
    },
  ).then((response) => {
    assertProject(projectId, response.project_id)
    assertProvider(canonicalProvider, response.provider)
    return response
  })
}

export function revokeProjectLlmConnection(
  conn: ServiceConnection,
  projectId: string,
  provider: LlmProvider,
  version: number,
  reason: string,
  options: RequestOptions = {},
): Promise<LlmConnectionSummary> {
  const canonicalProvider = llmProviderSchema.parse(provider)
  const body = revokeLlmConnectionRequestSchema.parse({
    project_id: projectId,
    version,
    reason,
  })
  return request(
    conn,
    `/v1/agents/llm-connections/${encodeURIComponent(canonicalProvider)}/revoke`,
    {
      method: 'POST',
      body,
      schema: llmConnectionSummarySchema,
      signal: options.signal,
    },
  ).then((response) => {
    assertProject(projectId, response.project_id)
    assertProvider(canonicalProvider, response.provider)
    return response
  })
}

export function providerDiscoveryErrorCode(error: unknown): ProviderDiscoveryErrorCode | null {
  if (!(error instanceof ApiError)) return null
  const parsed = providerDiscoveryErrorBodySchema.safeParse(error.body)
  return parsed.success ? parsed.data.detail.code : null
}

export function getAgentsSetup(
  conn: ServiceConnection,
  projectId: string,
  options: RequestOptions = {},
): Promise<AgentsSetup> {
  return request(conn, '/v1/agents/setup', {
    query: { project_id: projectId },
    schema: agentsSetupResponseSchema,
    signal: options.signal,
  }).then((response) => {
    assertProject(projectId, response.project_id)
    return response
  })
}

export function putAgentsSetup(
  conn: ServiceConnection,
  projectId: string,
  fastModel: AgentsModelSelection,
  reasoningModel: AgentsModelSelection,
  version: number,
  options: RequestOptions = {},
): Promise<AgentsSetup> {
  const body = putAgentsSetupRequestSchema.parse({
    project_id: projectId,
    fast_model: fastModel,
    reasoning_model: reasoningModel,
    version,
  })
  return request(conn, '/v1/agents/setup', {
    method: 'PUT',
    body,
    schema: agentsSetupResponseSchema,
    signal: options.signal,
  }).then((response) => {
    assertProject(projectId, response.project_id)
    return response
  })
}

export function deactivateAgentsSetup(
  conn: ServiceConnection,
  projectId: string,
  version: number,
  reason: string,
  options: RequestOptions = {},
): Promise<AgentsSetup> {
  const body = deactivateAgentsSetupRequestSchema.parse({
    project_id: projectId,
    version,
    reason,
  })
  return request(conn, '/v1/agents/setup/deactivate', {
    method: 'POST',
    body,
    schema: agentsSetupResponseSchema,
    signal: options.signal,
  }).then((response) => {
    assertProject(projectId, response.project_id)
    return response
  })
}
