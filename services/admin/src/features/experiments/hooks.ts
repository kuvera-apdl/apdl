import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import {
  createExperiment,
  deleteExperiment,
  experimentResults,
  listExperiments,
  updateExperiment,
  type ExperimentResultsParams,
} from '@/api/experiments'
import type { ExperimentCreate, ExperimentUpdate } from '@/api/types/experiments'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'

export function useExperimentsQuery() {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: queryKeys.experiments(active?.id ?? 'none'),
    enabled: active !== null,
    queryFn: ({ signal }) => listExperiments(serviceConnection(active!, 'config'), { signal }),
  })
}

function useInvalidateExperiments() {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  return () => {
    if (active) void queryClient.invalidateQueries({ queryKey: queryKeys.experiments(active.id) })
  }
}

export function useCreateExperimentMutation() {
  const { active } = useWorkspace()
  const invalidate = useInvalidateExperiments()
  return useMutation({
    mutationFn: (body: ExperimentCreate) =>
      createExperiment(serviceConnection(active!, 'config'), body),
    onSuccess: invalidate,
  })
}

export function useUpdateExperimentMutation(key: string) {
  const { active } = useWorkspace()
  const invalidate = useInvalidateExperiments()
  return useMutation({
    mutationFn: (body: ExperimentUpdate) =>
      updateExperiment(serviceConnection(active!, 'config'), key, body),
    onSuccess: invalidate,
  })
}

export function useDeleteExperimentMutation(key: string) {
  const { active } = useWorkspace()
  const invalidate = useInvalidateExperiments()
  return useMutation({
    mutationFn: () => deleteExperiment(serviceConnection(active!, 'config'), key),
    onSuccess: invalidate,
  })
}

/**
 * Manual-run experiment statistics (plan §5.4.3): heavy queries — results are
 * never auto-refreshed faster than 60s; the page offers an explicit refresh.
 */
export function useExperimentResultsQuery(
  experimentId: string,
  params: ExperimentResultsParams | null,
) {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: [
      active?.id ?? 'none',
      'experiments',
      experimentId,
      'results',
      JSON.stringify(params),
    ],
    enabled: active !== null && params !== null,
    staleTime: 60_000,
    queryFn: () => experimentResults(serviceConnection(active!, 'query'), experimentId, params!),
  })
}
