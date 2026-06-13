// Config-service client: flags admin reads (Phase 1 scope) plus the curl
// builders that keep every panel reproducible from the terminal.
import type { CurlSpec } from '@/lib/curl'

import { buildUrl, request, type ServiceConnection } from './http'
import {
  flagArchiveResponseSchema,
  flagAuditResponseSchema,
  flagCleanupResponseSchema,
  flagCleanupSchema,
  flagCreateResponseSchema,
  flagCreateSchema,
  flagDisableResponseSchema,
  flagDisableSchema,
  flagsListResponseSchema,
  flagUpdateResponseSchema,
  flagUpdateSchema,
  gateEvaluateResponseSchema,
  staleFlagsResponseSchema,
} from './schemas/flags'
import type {
  FlagArchiveResponse,
  FlagAuditResponse,
  FlagCleanup,
  FlagCleanupResponse,
  FlagCreate,
  FlagCreateResponse,
  FlagDisable,
  FlagDisableResponse,
  FlagsListResponse,
  FlagUpdate,
  FlagUpdateResponse,
  GateEvaluateRequest,
  GateEvaluateResponse,
  StaleFlagsResponse,
} from './types/flags'

export const AUDIT_LIMIT_DEFAULT = 50
export const AUDIT_LIMIT_MAX = 200

export function listFlags(
  conn: ServiceConnection,
  options: { includeArchived?: boolean; signal?: AbortSignal } = {},
): Promise<FlagsListResponse> {
  return request(conn, '/v1/admin/flags', {
    query: { include_archived: options.includeArchived ?? false },
    schema: flagsListResponseSchema,
    signal: options.signal,
  })
}

export function staleFlags(
  conn: ServiceConnection,
  options: { olderThanDays: number; signal?: AbortSignal },
): Promise<StaleFlagsResponse> {
  return request(conn, '/v1/admin/flags/stale', {
    query: { older_than_days: options.olderThanDays },
    schema: staleFlagsResponseSchema,
    signal: options.signal,
  })
}

export function flagAudit(
  conn: ServiceConnection,
  key: string,
  options: { limit?: number; signal?: AbortSignal } = {},
): Promise<FlagAuditResponse> {
  return request(conn, `/v1/admin/flags/${encodeURIComponent(key)}/audit`, {
    query: { limit: options.limit ?? AUDIT_LIMIT_DEFAULT },
    schema: flagAuditResponseSchema,
    signal: options.signal,
  })
}

// ---------- Flag writes (Phase 2) ----------
// Outgoing payloads are parsed against the canonical zod mirrors before they
// leave the client — a malformed payload fails fast and locally.

export function createFlag(conn: ServiceConnection, body: FlagCreate): Promise<FlagCreateResponse> {
  return request(conn, '/v1/admin/flags', {
    method: 'POST',
    body: flagCreateSchema.parse(body),
    schema: flagCreateResponseSchema,
  })
}

export function updateFlag(
  conn: ServiceConnection,
  key: string,
  body: FlagUpdate,
): Promise<FlagUpdateResponse> {
  return request(conn, `/v1/admin/flags/${encodeURIComponent(key)}`, {
    method: 'PUT',
    body: flagUpdateSchema.parse(body),
    schema: flagUpdateResponseSchema,
  })
}

export function disableFlag(
  conn: ServiceConnection,
  key: string,
  body: FlagDisable,
): Promise<FlagDisableResponse> {
  return request(conn, `/v1/admin/flags/${encodeURIComponent(key)}/disable`, {
    method: 'POST',
    body: flagDisableSchema.parse(body),
    schema: flagDisableResponseSchema,
  })
}

export function archiveFlag(conn: ServiceConnection, key: string): Promise<FlagArchiveResponse> {
  return request(conn, `/v1/admin/flags/${encodeURIComponent(key)}`, {
    method: 'DELETE',
    schema: flagArchiveResponseSchema,
  })
}

export function cleanupFlag(
  conn: ServiceConnection,
  key: string,
  body: FlagCleanup,
): Promise<FlagCleanupResponse> {
  return request(conn, `/v1/admin/flags/${encodeURIComponent(key)}/cleanup`, {
    method: 'POST',
    body: flagCleanupSchema.parse(body),
    schema: flagCleanupResponseSchema,
  })
}

/**
 * Server-verified evaluation (AD-4, optional): requires the workspace's
 * internal token; the server 403s for evaluation_mode "client" flags.
 */
export function evaluateFlagOnServer(
  conn: ServiceConnection,
  internalToken: string,
  body: GateEvaluateRequest,
): Promise<GateEvaluateResponse> {
  return request(conn, '/v1/evaluate', {
    method: 'POST',
    body,
    headers: { 'x-apdl-internal-token': internalToken },
    schema: gateEvaluateResponseSchema,
  })
}

function adminCurl(conn: ServiceConnection, path: string, query?: Record<string, string | number | boolean>): CurlSpec {
  return {
    method: 'GET',
    url: buildUrl(conn.baseUrl, path, query),
    headers: { 'X-API-Key': conn.apiKey },
  }
}

export function listFlagsCurl(conn: ServiceConnection, includeArchived = false): CurlSpec {
  return adminCurl(conn, '/v1/admin/flags', { include_archived: includeArchived })
}

export function staleFlagsCurl(conn: ServiceConnection, olderThanDays: number): CurlSpec {
  return adminCurl(conn, '/v1/admin/flags/stale', { older_than_days: olderThanDays })
}

export function flagAuditCurl(conn: ServiceConnection, key: string, limit = AUDIT_LIMIT_DEFAULT): CurlSpec {
  return adminCurl(conn, `/v1/admin/flags/${encodeURIComponent(key)}/audit`, { limit })
}

export function writeCurl(
  conn: ServiceConnection,
  method: 'POST' | 'PUT' | 'DELETE',
  path: string,
  body?: unknown,
): CurlSpec {
  const headers: Record<string, string> = {
    'X-API-Key': conn.apiKey,
    'x-apdl-actor': conn.actor,
  }
  if (body !== undefined) headers['Content-Type'] = 'application/json'
  return { method, url: buildUrl(conn.baseUrl, path), headers, ...(body !== undefined ? { body } : {}) }
}

export function createFlagCurl(conn: ServiceConnection, body: FlagCreate): CurlSpec {
  return writeCurl(conn, 'POST', '/v1/admin/flags', body)
}

export function updateFlagCurl(conn: ServiceConnection, key: string, body: FlagUpdate): CurlSpec {
  return writeCurl(conn, 'PUT', `/v1/admin/flags/${encodeURIComponent(key)}`, body)
}

export function disableFlagCurl(conn: ServiceConnection, key: string, body: FlagDisable): CurlSpec {
  return writeCurl(conn, 'POST', `/v1/admin/flags/${encodeURIComponent(key)}/disable`, body)
}

export function archiveFlagCurl(conn: ServiceConnection, key: string): CurlSpec {
  return writeCurl(conn, 'DELETE', `/v1/admin/flags/${encodeURIComponent(key)}`)
}

export function cleanupFlagCurl(conn: ServiceConnection, key: string, body: FlagCleanup): CurlSpec {
  return writeCurl(conn, 'POST', `/v1/admin/flags/${encodeURIComponent(key)}/cleanup`, body)
}

/** Example create call shown on empty states. */
export function createFlagExampleCurl(conn: ServiceConnection): CurlSpec {
  return {
    method: 'POST',
    url: buildUrl(conn.baseUrl, '/v1/admin/flags'),
    headers: {
      'X-API-Key': conn.apiKey,
      'x-apdl-actor': conn.actor,
      'Content-Type': 'application/json',
    },
    body: {
      key: 'my-first-flag',
      name: 'My first flag',
      default_variant: 'control',
      variants: [
        { key: 'control', weight: 1 },
        { key: 'treatment', weight: 1 },
      ],
      fallthrough: { rollout: { percentage: 0, bucket_by: 'user_id' } },
    },
  }
}
