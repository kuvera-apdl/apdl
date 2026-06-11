// Config-service client: flags admin reads (Phase 1 scope) plus the curl
// builders that keep every panel reproducible from the terminal.
import type { CurlSpec } from '@/lib/curl'

import { buildUrl, request, type ServiceConnection } from './http'
import {
  flagAuditResponseSchema,
  flagsListResponseSchema,
  staleFlagsResponseSchema,
} from './schemas/flags'
import type { FlagAuditResponse, FlagsListResponse, StaleFlagsResponse } from './types/flags'

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

/** Example create call shown on empty states (creating flags lands in Phase 2). */
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
