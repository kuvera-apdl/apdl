import { useQueries, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  getAgentsSetup,
  getProjectLlmModels,
  listProjectLlmConnections,
} from '@/api/agentsSetup'
import type {
  AgentsSetup,
  LlmConnectionSummary,
  LlmModelInventory,
} from '@/api/types/agents-setup'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'

export function useAgentsSetup(enabled = true) {
  const { active, projectId } = useWorkspace()
  return useQuery({
    queryKey:
      active && projectId
        ? queryKeys.agentsSetup(active.id, projectId)
        : ['agents-setup-idle'],
    enabled: enabled && active !== null && projectId !== null,
    queryFn: ({ signal }) =>
      getAgentsSetup(serviceConnection(active!, 'agents'), projectId!, {
        signal,
      }),
  })
}

export function useAgentsConnections() {
  const { active, projectId } = useWorkspace()
  return useQuery({
    queryKey:
      active && projectId
        ? queryKeys.agentsConnections(active.id, projectId)
        : ['agents-connections-idle'],
    enabled: active !== null && projectId !== null,
    queryFn: ({ signal }) =>
      listProjectLlmConnections(
        serviceConnection(active!, 'agents'),
        projectId!,
        { signal },
      ),
  })
}

export function useAgentsModelInventory(
  connection: LlmConnectionSummary | null,
) {
  const { active, projectId } = useWorkspace()
  return useQuery<LlmModelInventory>({
    queryKey:
      active && projectId && connection
        ? queryKeys.agentsModels(
            active.id,
            projectId,
            connection.provider,
            connection.version,
            connection.inventory_version,
          )
        : ['agents-models-idle'],
    enabled:
      active !== null &&
      projectId !== null &&
      connection?.state === 'active',
    queryFn: ({ signal }) =>
      getProjectLlmModels(
        serviceConnection(active!, 'agents'),
        projectId!,
        connection!.provider,
        { signal },
      ),
  })
}

export function useAgentsModelInventories(
  connections: readonly LlmConnectionSummary[],
) {
  const { active, projectId } = useWorkspace()
  const currentConnections = connections.filter(
    (connection) => connection.state === 'active',
  )
  return useQueries({
    queries: currentConnections.map((connection) => ({
      queryKey:
        active && projectId
          ? queryKeys.agentsModels(
              active.id,
              projectId,
              connection.provider,
              connection.version,
              connection.inventory_version,
            )
          : ['agents-models-idle', connection.provider],
      enabled: active !== null && projectId !== null,
      queryFn: ({ signal }: { signal: AbortSignal }) =>
        getProjectLlmModels(
          serviceConnection(active!, 'agents'),
          projectId!,
          connection.provider,
          { signal },
        ),
    })),
  })
}

export function useInvalidateAgentsConnections(): () => Promise<void> {
  const { active, projectId } = useWorkspace()
  const queryClient = useQueryClient()
  return async () => {
    if (!active || !projectId) return
    await queryClient.invalidateQueries({
      queryKey: queryKeys.agentsConnectionsPrefix(active.id, projectId),
    })
  }
}

export function useInvalidateAgentsSetup(): (
  committedSetup?: AgentsSetup,
) => Promise<void> {
  const { active, projectId } = useWorkspace()
  const queryClient = useQueryClient()
  return async (committedSetup?: AgentsSetup) => {
    if (!active || !projectId) return
    if (committedSetup) {
      queryClient.setQueryData(
        queryKeys.agentsSetup(active.id, projectId),
        committedSetup,
      )
    }
    await Promise.all([
      queryClient.invalidateQueries({
        queryKey: queryKeys.agentsSetup(active.id, projectId),
      }),
      queryClient.invalidateQueries({
        queryKey: queryKeys.agentsConnectionsPrefix(active.id, projectId),
      }),
    ])
  }
}
