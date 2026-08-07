// Codegen-service client. The same-origin admin proxy injects a project-scoped
// API key after authorizing the human session, project, and role.
import { ApiError, request, type ServiceConnection } from './http'
import {
  changesetListSchema,
  changesetSchema,
  githubRepositoryAuthorizationCompleteRequestSchema,
  githubRepositoryAuthorizationIdSchema,
  githubRepositoryAuthorizationSchema,
  githubRepositoryAuthorizationStartRequestSchema,
  githubRepositoryAuthorizationStartSchema,
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
  GitHubRepositoryAuthorization,
  GitHubRepositoryAuthorizationStart,
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

const GITHUB_REPOSITORY_AUTHORIZATIONS_PATH = '/v1/github/repository-authorizations'

export function startGitHubRepositoryAuthorization(
  conn: ServiceConnection,
  projectId: string,
): Promise<GitHubRepositoryAuthorizationStart> {
  const body = githubRepositoryAuthorizationStartRequestSchema.parse({ project_id: projectId })
  return request(conn, GITHUB_REPOSITORY_AUTHORIZATIONS_PATH, {
    method: 'POST',
    body,
    schema: githubRepositoryAuthorizationStartSchema,
  })
}

export async function getGitHubRepositoryAuthorization(
  conn: ServiceConnection,
  projectId: string,
  authorizationId: string,
  options: { signal?: AbortSignal } = {},
): Promise<GitHubRepositoryAuthorization> {
  const { project_id: canonicalProjectId } =
    githubRepositoryAuthorizationStartRequestSchema.parse({ project_id: projectId })
  const canonicalAuthorizationId = githubRepositoryAuthorizationIdSchema.parse(authorizationId)
  const authorization = await request(
    conn,
    `${GITHUB_REPOSITORY_AUTHORIZATIONS_PATH}/${encodeURIComponent(canonicalAuthorizationId)}`,
    {
      query: { project_id: canonicalProjectId },
      schema: githubRepositoryAuthorizationSchema,
      signal: options.signal,
    },
  )
  if (
    authorization.project_id !== canonicalProjectId ||
    authorization.authorization_id !== canonicalAuthorizationId
  ) {
    throw new ApiError(
      200,
      'schema_mismatch',
      'Repository authorization does not match the requested project and authorization id',
      authorization,
    )
  }
  return authorization
}

export async function completeGitHubRepositoryAuthorization(
  conn: ServiceConnection,
  projectId: string,
  authorizationId: string,
  candidateId: string,
): Promise<RepoConnection> {
  const canonicalAuthorizationId = githubRepositoryAuthorizationIdSchema.parse(authorizationId)
  const body = githubRepositoryAuthorizationCompleteRequestSchema.parse({
    project_id: projectId,
    candidate_id: candidateId,
  })
  const connection = await request(
    conn,
    `${GITHUB_REPOSITORY_AUTHORIZATIONS_PATH}/${encodeURIComponent(canonicalAuthorizationId)}/complete`,
    {
      method: 'POST',
      body,
      schema: repoConnectionSchema,
    },
  )
  if (connection.project_id !== body.project_id) {
    throw new ApiError(
      200,
      'schema_mismatch',
      'Repository connection does not match the requested project',
      connection,
    )
  }
  return connection
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
