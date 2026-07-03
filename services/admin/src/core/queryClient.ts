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
  repoConnection: (wsId: string) => [wsId, 'repo-connection'] as const,
  changeset: (wsId: string, id: string) => [wsId, 'changeset', id] as const,
  // One prefix covers list, detail, and the combined definitions listing —
  // a custom-agent write invalidates all three.
  customAgentsPrefix: (wsId: string) => [wsId, 'custom-agents'] as const,
  customAgents: (wsId: string, projectId: string) =>
    [wsId, 'custom-agents', 'list', projectId] as const,
  customAgent: (wsId: string, projectId: string, agentId: string) =>
    [wsId, 'custom-agents', 'detail', projectId, agentId] as const,
  agentDefinitions: (wsId: string, projectId: string) =>
    [wsId, 'custom-agents', 'definitions', projectId] as const,
}
