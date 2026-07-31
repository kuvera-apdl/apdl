// Codegen-service client. The same-origin admin proxy injects a project-scoped
// API key after authorizing the human session, project, and role.
import { ApiError, request, type ServiceConnection } from './http'
import {
  changesetListSchema,
  changesetSchema,
  repoConnectionSchema,
} from './schemas/codegen'
import {
  codegenLlmConnectionDetailSchema,
  codegenLlmConnectionListSchema,
  codegenLlmConnectionSummarySchema,
  codegenLlmModelInventorySchema,
  codegenLlmProjectIdSchema,
  codegenLlmProviderSchema,
  putCodegenLlmConnectionRequestSchema,
  refreshCodegenLlmConnectionRequestSchema,
  revokeCodegenLlmConnectionRequestSchema,
} from './schemas/codegen-llm-connections'
import { changesetObservationHistorySchema } from './schemas/codegen-observations'
import { runtimeEvidenceObservationListSchema } from './schemas/codegen-runtime'
import type {
  Changeset,
  ChangesetObservationHistory,
  RepoConnection,
  RuntimeEvidenceObservation,
} from './types/codegen'
import type {
  CodegenLlmConnectionDetail,
  CodegenLlmConnectionList,
  CodegenLlmConnectionSummary,
  CodegenLlmModelInventory,
  CodegenLlmProvider,
  PutCodegenLlmConnectionRequest,
  RefreshCodegenLlmConnectionRequest,
  RevokeCodegenLlmConnectionRequest,
} from './types/codegen-llm-connections'

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

export function getChangesetObservations(
  conn: ServiceConnection,
  changesetId: string,
  options: { signal?: AbortSignal } = {},
): Promise<ChangesetObservationHistory> {
  return request(
    conn,
    `/v1/changesets/${encodeURIComponent(changesetId)}/observations`,
    {
      schema: changesetObservationHistorySchema,
      signal: options.signal,
    },
  )
}

export function getRuntimeEvidenceObservations(
  conn: ServiceConnection,
  changesetId: string,
  options: { signal?: AbortSignal; limit?: number } = {},
): Promise<RuntimeEvidenceObservation[]> {
  return request(
    conn,
    `/v1/changesets/${encodeURIComponent(changesetId)}/runtime-observations`,
    {
      query: { limit: options.limit ?? 50 },
      schema: runtimeEvidenceObservationListSchema,
      signal: options.signal,
    },
  )
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

/** Read a project's active verified repository grant; `null` means no grant (404). */
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

function codegenLlmConnectionPath(provider?: CodegenLlmProvider): string {
  const base = '/v1/llm-connections'
  return provider ? `${base}/${codegenLlmProviderSchema.parse(provider)}` : base
}

export function listCodegenLlmConnections(
  conn: ServiceConnection,
  projectId: string,
  options: { signal?: AbortSignal } = {},
): Promise<CodegenLlmConnectionList> {
  const canonicalProjectId = codegenLlmProjectIdSchema.parse(projectId)
  return request(conn, codegenLlmConnectionPath(), {
    query: { project_id: canonicalProjectId },
    schema: codegenLlmConnectionListSchema,
    signal: options.signal,
  }).then((result) => {
    if (result.project_id !== canonicalProjectId) {
      throw new Error('Codegen LLM connection list crossed project authority')
    }
    return result
  })
}

export function putCodegenLlmConnection(
  conn: ServiceConnection,
  provider: CodegenLlmProvider,
  requestBody: PutCodegenLlmConnectionRequest,
): Promise<CodegenLlmConnectionDetail> {
  const canonicalProvider = codegenLlmProviderSchema.parse(provider)
  const body = putCodegenLlmConnectionRequestSchema.parse(requestBody)
  return request(conn, codegenLlmConnectionPath(canonicalProvider), {
    method: 'PUT',
    body,
    schema: codegenLlmConnectionDetailSchema,
    // Provider authentication failures do not invalidate the Admin session.
    redirectOnUnauthorized: false,
  }).then((result) => {
    if (
      result.project_id !== body.project_id ||
      result.provider !== canonicalProvider
    ) {
      throw new Error('Codegen LLM connection response crossed project authority')
    }
    return result
  })
}

export function getCodegenLlmModels(
  conn: ServiceConnection,
  provider: CodegenLlmProvider,
  projectId: string,
  options: { signal?: AbortSignal } = {},
): Promise<CodegenLlmModelInventory> {
  const canonicalProvider = codegenLlmProviderSchema.parse(provider)
  const canonicalProjectId = codegenLlmProjectIdSchema.parse(projectId)
  return request(conn, `${codegenLlmConnectionPath(canonicalProvider)}/models`, {
    query: { project_id: canonicalProjectId },
    schema: codegenLlmModelInventorySchema,
    signal: options.signal,
  }).then((result) => {
    if (
      result.project_id !== canonicalProjectId ||
      result.provider !== canonicalProvider
    ) {
      throw new Error('Codegen LLM model inventory crossed project authority')
    }
    return result
  })
}

export function refreshCodegenLlmModels(
  conn: ServiceConnection,
  provider: CodegenLlmProvider,
  requestBody: RefreshCodegenLlmConnectionRequest,
): Promise<CodegenLlmConnectionDetail> {
  const canonicalProvider = codegenLlmProviderSchema.parse(provider)
  const body = refreshCodegenLlmConnectionRequestSchema.parse(requestBody)
  return request(
    conn,
    `${codegenLlmConnectionPath(canonicalProvider)}/refresh-models`,
    {
      method: 'POST',
      body,
      schema: codegenLlmConnectionDetailSchema,
      // Revalidation can surface a provider credential rejection.
      redirectOnUnauthorized: false,
    },
  ).then((result) => {
    if (
      result.project_id !== body.project_id ||
      result.provider !== canonicalProvider
    ) {
      throw new Error('Codegen LLM model refresh crossed project authority')
    }
    return result
  })
}

export function revokeCodegenLlmConnection(
  conn: ServiceConnection,
  provider: CodegenLlmProvider,
  requestBody: RevokeCodegenLlmConnectionRequest,
): Promise<CodegenLlmConnectionSummary> {
  const canonicalProvider = codegenLlmProviderSchema.parse(provider)
  const body = revokeCodegenLlmConnectionRequestSchema.parse(requestBody)
  return request(conn, `${codegenLlmConnectionPath(canonicalProvider)}/revoke`, {
    method: 'POST',
    body,
    schema: codegenLlmConnectionSummarySchema,
  }).then((result) => {
    if (
      result.project_id !== body.project_id ||
      result.provider !== canonicalProvider
    ) {
      throw new Error('Codegen LLM connection revocation crossed project authority')
    }
    return result
  })
}
