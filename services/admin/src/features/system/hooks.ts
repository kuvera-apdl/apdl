import { useQuery } from '@tanstack/react-query'

import { checkService, type HealthServiceName, type ServiceHealth } from '@/api/health'
import { queryKeys } from '@/core/queryClient'
import { serviceBaseUrl, useWorkspace } from '@/core/workspace'

const HEALTH_POLL_MS = 10_000

export function useServiceHealthQuery(service: HealthServiceName) {
  const { active } = useWorkspace()
  return useQuery<ServiceHealth>({
    queryKey: queryKeys.serviceHealth(active?.id ?? 'none', service),
    enabled: active !== null,
    refetchInterval: HEALTH_POLL_MS,
    staleTime: 0,
    queryFn: () =>
      checkService({
        service,
        baseUrl: serviceBaseUrl(active!, service),
      }),
  })
}
