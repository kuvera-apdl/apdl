import { apiCurl, request, type ServiceConnection } from './http'
import {
  agentDefinitionsResponseSchema,
  approvalRequestSchema,
  approvalResponseSchema,
  customAgentListSchema,
  customAgentSchema,
  customAgentSpecSchema,
  runAuditResponseSchema,
  runResultsSchema,
  runsListResponseSchema,
  runStatusSchema,
  testRunRequestSchema,
  testRunResponseSchema,
  triggerRequestSchema,
  triggerResponseSchema,
} from './schemas/agents'
import type {
  AgentDefinitionsResponse,
  ApprovalRequest,
  ApprovalResponse,
  CustomAgent,
  CustomAgentSpec,
  RunAuditResponse,
  RunResults,
  RunsListResponse,
  RunStatus,
  TestRunRequest,
  TestRunResponse,
  TriggerRequest,
  TriggerResponse,
} from './types/agents'
import type { CurlSpec } from '@/lib/curl'

export function triggerRun(conn: ServiceConnection, body: TriggerRequest): Promise<TriggerResponse> {
  return request(conn, '/v1/agents/trigger', {
    method: 'POST',
    body: triggerRequestSchema.parse(body),
    schema: triggerResponseSchema,
  })
}

export function runStatus(
  conn: ServiceConnection,
  runId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RunStatus> {
  return request(conn, `/v1/agents/${encodeURIComponent(runId)}/status`, {
    schema: runStatusSchema,
    signal: options.signal,
  })
}

export function approveRun(
  conn: ServiceConnection,
  runId: string,
  body: ApprovalRequest,
): Promise<ApprovalResponse> {
  return request(conn, `/v1/agents/${encodeURIComponent(runId)}/approve`, {
    method: 'POST',
    body: approvalRequestSchema.parse(body),
    schema: approvalResponseSchema,
  })
}

export interface ListRunsParams {
  projectId: string
  status?: string
  limit?: number
}

export function listRuns(
  conn: ServiceConnection,
  params: ListRunsParams,
  options: { signal?: AbortSignal } = {},
): Promise<RunsListResponse> {
  return request(conn, '/v1/agents/runs', {
    query: { project_id: params.projectId, status: params.status, limit: params.limit ?? 50 },
    schema: runsListResponseSchema,
    signal: options.signal,
  })
}

export function runResults(
  conn: ServiceConnection,
  runId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RunResults> {
  return request(conn, `/v1/agents/${encodeURIComponent(runId)}/results`, {
    schema: runResultsSchema,
    signal: options.signal,
  })
}

export function runAudit(
  conn: ServiceConnection,
  runId: string,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<RunAuditResponse> {
  return request(conn, `/v1/agents/${encodeURIComponent(runId)}/audit`, {
    query: { limit: options.limit ?? 100 },
    schema: runAuditResponseSchema,
    signal: options.signal,
  })
}

// ---------- Custom agents (routers/custom_agents.py) ----------

export function listAgentDefinitions(
  conn: ServiceConnection,
  projectId: string,
  options: { signal?: AbortSignal } = {},
): Promise<AgentDefinitionsResponse> {
  return request(conn, '/v1/agents/definitions', {
    query: { project_id: projectId },
    schema: agentDefinitionsResponseSchema,
    signal: options.signal,
  })
}

export function listCustomAgents(
  conn: ServiceConnection,
  projectId: string,
  options: { includeArchived?: boolean; signal?: AbortSignal } = {},
): Promise<CustomAgent[]> {
  return request(conn, '/v1/agents/custom', {
    query: { project_id: projectId, include_archived: options.includeArchived ?? false },
    schema: customAgentListSchema,
    signal: options.signal,
  })
}

export function getCustomAgent(
  conn: ServiceConnection,
  projectId: string,
  agentId: string,
  options: { signal?: AbortSignal } = {},
): Promise<CustomAgent> {
  return request(conn, `/v1/agents/custom/${encodeURIComponent(agentId)}`, {
    query: { project_id: projectId },
    schema: customAgentSchema,
    signal: options.signal,
  })
}

export function createCustomAgent(
  conn: ServiceConnection,
  projectId: string,
  body: CustomAgentSpec,
): Promise<CustomAgent> {
  return request(conn, '/v1/agents/custom', {
    method: 'POST',
    query: { project_id: projectId },
    body: customAgentSpecSchema.parse(body),
    schema: customAgentSchema,
  })
}

export function updateCustomAgent(
  conn: ServiceConnection,
  projectId: string,
  agentId: string,
  body: CustomAgentSpec,
): Promise<CustomAgent> {
  return request(conn, `/v1/agents/custom/${encodeURIComponent(agentId)}`, {
    method: 'PUT',
    query: { project_id: projectId },
    body: customAgentSpecSchema.parse(body),
    schema: customAgentSchema,
  })
}

export function archiveCustomAgent(
  conn: ServiceConnection,
  projectId: string,
  agentId: string,
): Promise<void> {
  return request(conn, `/v1/agents/custom/${encodeURIComponent(agentId)}`, {
    method: 'DELETE',
    query: { project_id: projectId },
  })
}

export function testCustomAgent(
  conn: ServiceConnection,
  body: TestRunRequest,
): Promise<TestRunResponse> {
  return request(conn, '/v1/agents/custom/test', {
    method: 'POST',
    body: testRunRequestSchema.parse(body),
    schema: testRunResponseSchema,
  })
}

export function triggerRunCurl(conn: ServiceConnection, body: TriggerRequest): CurlSpec {
  return apiCurl(conn, 'POST', '/v1/agents/trigger', { body })
}

export function runStatusCurl(conn: ServiceConnection, runId: string): CurlSpec {
  return apiCurl(conn, 'GET', `/v1/agents/${encodeURIComponent(runId)}/status`)
}

export function listRunsCurl(conn: ServiceConnection, params: ListRunsParams): CurlSpec {
  return apiCurl(conn, 'GET', '/v1/agents/runs', {
    query: { project_id: params.projectId, status: params.status, limit: params.limit ?? 50 },
  })
}

export function runResultsCurl(conn: ServiceConnection, runId: string): CurlSpec {
  return apiCurl(conn, 'GET', `/v1/agents/${encodeURIComponent(runId)}/results`)
}

export function createCustomAgentCurl(
  conn: ServiceConnection,
  projectId: string,
  body: CustomAgentSpec,
): CurlSpec {
  return apiCurl(conn, 'POST', '/v1/agents/custom', {
    query: { project_id: projectId },
    body,
  })
}

export function testCustomAgentCurl(conn: ServiceConnection, body: TestRunRequest): CurlSpec {
  return apiCurl(conn, 'POST', '/v1/agents/custom/test', { body })
}

export function listCustomAgentsCurl(conn: ServiceConnection, projectId: string): CurlSpec {
  return apiCurl(conn, 'GET', '/v1/agents/custom', { query: { project_id: projectId } })
}
