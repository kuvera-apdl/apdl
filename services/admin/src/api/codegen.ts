// Codegen-service client. The same-origin admin proxy injects the internal
// service token after authorizing the human session, project, and role.
import { ApiError, request, type ServiceConnection } from './http'
import {
  changesetListSchema,
  changesetSchema,
  repoConnectionSchema,
} from './schemas/codegen'
import type {
  Changeset,
  RepoConnection,
} from './types/codegen'

export interface ListChangesetsParams {
  projectId: string
  limit?: number
}

export function listChangesets(
  conn: ServiceConnection,
  params: ListChangesetsParams,
  options: { signal?: AbortSignal } = {},
): Promise<Changeset[]> {
  return request(conn, '/v1/changesets', {
    query: { project_id: params.projectId, limit: params.limit ?? 50 },
    schema: changesetListSchema,
    signal: options.signal,
  })
}

export function getChangeset(
  conn: ServiceConnection,
  changesetId: string,
  options: { signal?: AbortSignal } = {},
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}`, {
    schema: changesetSchema,
    signal: options.signal,
  })
}

export function abandonChangeset(
  conn: ServiceConnection,
  changesetId: string,
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/abandon`, {
    method: 'POST',
    schema: changesetSchema,
  })
}

/** Resolve a project's repo binding; `null` means "not connected" (404). */
export async function getRepoConnection(
  conn: ServiceConnection,
  projectId: string,
  options: { signal?: AbortSignal } = {},
): Promise<RepoConnection | null> {
  try {
    return await request(conn, `/v1/connections/${encodeURIComponent(projectId)}`, {
      schema: repoConnectionSchema,
      signal: options.signal,
    })
  } catch (error) {
    if (error instanceof ApiError && error.status === 404) return null
    throw error
  }
}

export function revertChangeset(
  conn: ServiceConnection,
  changesetId: string,
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/revert`, {
    method: 'POST',
    schema: changesetSchema,
  })
}

/** Re-run a failed changeset; returns the NEW changeset enqueued for the retry. */
export function retryChangeset(
  conn: ServiceConnection,
  changesetId: string,
): Promise<Changeset> {
  return request(conn, `/v1/changesets/${encodeURIComponent(changesetId)}/retry`, {
    method: 'POST',
    schema: changesetSchema,
  })
}
