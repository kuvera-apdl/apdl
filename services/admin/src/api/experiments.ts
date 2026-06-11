// Experiments: CRUD on the config service, results from the query service.
import { apiCurl, request, type ServiceConnection } from './http'
import {
  experimentCreateResponseSchema,
  experimentCreateSchema,
  experimentDeleteResponseSchema,
  experimentResultSchema,
  experimentsListResponseSchema,
  experimentUpdateResponseSchema,
  experimentUpdateSchema,
} from './schemas/experiments'
import type {
  AnalysisMethod,
  ExperimentCreate,
  ExperimentCreateResponse,
  ExperimentDeleteResponse,
  ExperimentResult,
  ExperimentsListResponse,
  ExperimentUpdate,
  ExperimentUpdateResponse,
} from './types/experiments'
import type { CurlSpec } from '@/lib/curl'

export function listExperiments(
  conn: ServiceConnection,
  options: { signal?: AbortSignal } = {},
): Promise<ExperimentsListResponse> {
  return request(conn, '/v1/admin/experiments', {
    schema: experimentsListResponseSchema,
    signal: options.signal,
  })
}

export function createExperiment(
  conn: ServiceConnection,
  body: ExperimentCreate,
): Promise<ExperimentCreateResponse> {
  return request(conn, '/v1/admin/experiments', {
    method: 'POST',
    body: experimentCreateSchema.parse(body),
    schema: experimentCreateResponseSchema,
  })
}

export function updateExperiment(
  conn: ServiceConnection,
  key: string,
  body: ExperimentUpdate,
): Promise<ExperimentUpdateResponse> {
  return request(conn, `/v1/admin/experiments/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: experimentUpdateSchema.parse(body),
    schema: experimentUpdateResponseSchema,
  })
}

export function deleteExperiment(
  conn: ServiceConnection,
  key: string,
): Promise<ExperimentDeleteResponse> {
  return request(conn, `/v1/admin/experiments/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    schema: experimentDeleteResponseSchema,
  })
}

export interface ExperimentResultsParams {
  projectId: string
  metric: string
  flagKey: string
  method: AnalysisMethod
}

/** Query-service connection, not config. project_id is a query param here. */
export function experimentResults(
  queryConn: ServiceConnection,
  experimentId: string,
  params: ExperimentResultsParams,
): Promise<ExperimentResult> {
  return request(queryConn, `/v1/query/experiment/${encodeURIComponent(experimentId)}`, {
    query: {
      metric: params.metric,
      flag_key: params.flagKey,
      method: params.method,
      project_id: params.projectId,
    },
    schema: experimentResultSchema,
  })
}

export function listExperimentsCurl(conn: ServiceConnection): CurlSpec {
  return apiCurl(conn, 'GET', '/v1/admin/experiments')
}

export function experimentResultsCurl(
  queryConn: ServiceConnection,
  experimentId: string,
  params: ExperimentResultsParams,
): CurlSpec {
  return apiCurl(queryConn, 'GET', `/v1/query/experiment/${encodeURIComponent(experimentId)}`, {
    query: {
      metric: params.metric,
      flag_key: params.flagKey,
      method: params.method,
      project_id: params.projectId,
    },
  })
}
