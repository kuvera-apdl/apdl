// Codegen-service client. Guarded by the shared internal token (not the project
// API key), passed via the X-APDL-Internal-Token header.
import { request, type ServiceConnection } from './http'
import { changesetListSchema, changesetSchema, mergeRequestSchema } from './schemas/codegen'
import type { Changeset, MergeRequest } from './types/codegen'

function authHeaders(internalToken: string): Record<string, string> | undefined {
  return internalToken ? { 'X-APDL-Internal-Token': internalToken } : undefined
}

export interface ListChangesetsParams {
  projectId: string
  limit?: number
}

export function listChangesets(
  conn: ServiceConnection,
  internalToken: string,
  params: ListChangesetsParams,
  options: { signal?: AbortSignal } = {},
): Promise<Changeset[]> {
  return request(conn, '/v1/changesets', {
    query: { project_id: params.projectId, limit: params.limit ?? 50 },
    schema: changesetListSchema,
    headers: authHeaders(internalToken),
    signal: options.signal,
  })
}

export function getChangeset(
  conn: ServiceConnection,
  internalToken: string,
  changesetId: string,
  options: { signal?: AbortSignal } = {},
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}`, {
    schema: changesetSchema,
    headers: authHeaders(internalToken),
    signal: options.signal,
  })
}

export function mergeChangeset(
  conn: ServiceConnection,
  internalToken: string,
  changesetId: string,
  body: MergeRequest = { merge_method: 'squash' },
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/merge`, {
    method: 'POST',
    body: mergeRequestSchema.parse(body),
    schema: changesetSchema,
    headers: authHeaders(internalToken),
  })
}

export function abandonChangeset(
  conn: ServiceConnection,
  internalToken: string,
  changesetId: string,
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/abandon`, {
    method: 'POST',
    schema: changesetSchema,
    headers: authHeaders(internalToken),
  })
}

export function revertChangeset(
  conn: ServiceConnection,
  internalToken: string,
  changesetId: string,
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/revert`, {
    method: 'POST',
    schema: changesetSchema,
    headers: authHeaders(internalToken),
  })
}
