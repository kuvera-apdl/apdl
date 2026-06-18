import { QueryClient } from '@tanstack/react-query'

// Retries live in api/http.ts (GETs only); TanStack must not stack its own.
// staleTime 15s for config data — SSE invalidation keeps it fresh (AD-5).
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 15_000,
        refetchOnWindowFocus: true,
      },
      mutations: {
        retry: false,
      },
    },
  })
}

// Keys are namespaced by workspace id. Audit keys live under the flags prefix
// so one invalidation covers list, detail, and audit after a flag_update.
export const queryKeys = {
  flagsPrefix: (wsId: string) => [wsId, 'flags'] as const,
  flags: (wsId: string) => [wsId, 'flags', 'list'] as const,
  flagAudit: (wsId: string, key: string, limit: number) =>
    [wsId, 'flags', 'audit', key, limit] as const,
  staleFlagsPrefix: (wsId: string) => [wsId, 'flags-stale'] as const,
  staleFlags: (wsId: string, olderThanDays: number) => [wsId, 'flags-stale', olderThanDays] as const,
  serviceHealth: (wsId: string, service: string) => [wsId, 'health', service] as const,
  experiments: (wsId: string) => [wsId, 'experiments'] as const,
  changesets: (wsId: string) => [wsId, 'changesets'] as const,
}
