// Agents-service client. The query/agents services have no auth today (§1.4)
// — the canonical headers are still sent for forward-compatibility (gap G9).
import { apiCurl, request, type ServiceConnection } from './http'
import {
  approvalRequestSchema,
  approvalResponseSchema,
  runAuditResponseSchema,
  runResultsSchema,
  runsListResponseSchema,
  runStatusSchema,
  triggerRequestSchema,
  triggerResponseSchema,
} from './schemas/agents'
import type {
  ApprovalRequest,
  ApprovalResponse,
  RunAuditResponse,
  RunResults,
  RunsListResponse,
  RunStatus,
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
