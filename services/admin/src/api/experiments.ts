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
import { projectIdSchema } from './schemas/query'
import type {
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
  version: number,
): Promise<ExperimentDeleteResponse> {
  return request(conn, `/v1/admin/experiments/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    query: { version },
    schema: experimentDeleteResponseSchema,
  })
}

export interface ExperimentResultsParams {
  projectId: string
}

function canonicalProjectId(params: ExperimentResultsParams): string {
  return projectIdSchema.parse(params.projectId)
}

/** Query-service connection, not config. project_id is a query param here. */
export function experimentResults(
  queryConn: ServiceConnection,
  experimentKey: string,
  params: ExperimentResultsParams,
  options: { signal?: AbortSignal } = {},
): Promise<ExperimentResult> {
  return request(queryConn, `/v1/query/experiment/${encodeURIComponent(experimentKey)}`, {
    query: {
      project_id: canonicalProjectId(params),
    },
    schema: experimentResultSchema,
    signal: options.signal,
  })
}

export function listExperimentsCurl(conn: ServiceConnection): CurlSpec {
  return apiCurl(conn, 'GET', '/v1/admin/experiments')
}

export function experimentResultsCurl(
  queryConn: ServiceConnection,
  experimentKey: string,
  params: ExperimentResultsParams,
): CurlSpec {
  return apiCurl(queryConn, 'GET', `/v1/query/experiment/${encodeURIComponent(experimentKey)}`, {
    query: {
      project_id: canonicalProjectId(params),
    },
  })
}
