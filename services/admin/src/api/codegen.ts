// Codegen-service client. Guarded by the shared internal token (not the project
// API key), passed via the X-APDL-Internal-Token header.
import { ApiError, request, type ServiceConnection } from './http'
import {
  changesetListSchema,
  changesetSchema,
  mergeRequestSchema,
  repoConnectionCreateSchema,
  repoConnectionSchema,
} from './schemas/codegen'
import type { Changeset, MergeRequest, RepoConnection, RepoConnectionCreate } from './types/codegen'

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

/** Resolve a project's repo binding; `null` means "not connected" (404). */
export async function getRepoConnection(
  conn: ServiceConnection,
  internalToken: string,
  projectId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RepoConnection | null> {
  try {
    return await request(conn, `/v1/connections/${encodeURIComponent(projectId)}`, {
      schema: repoConnectionSchema,
      headers: authHeaders(internalToken),
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

export function connectRepo(
  conn: ServiceConnection,
  internalToken: string,
  body: RepoConnectionCreate,
): Promise<RepoConnection> {
  return request(conn, '/v1/connections', {
    method: 'POST',
    body: repoConnectionCreateSchema.parse(body),
    schema: repoConnectionSchema,
    headers: authHeaders(internalToken),
  })
}

export function disconnectRepo(
  conn: ServiceConnection,
  internalToken: string,
  projectId: string,
): Promise<void> {
  return request(conn, `/v1/connections/${encodeURIComponent(projectId)}`, {
    method: 'DELETE',
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
