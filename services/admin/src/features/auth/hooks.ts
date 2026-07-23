import { useQuery } from '@tanstack/react-query'

import { getAuthCapabilities } from '@/api/auth'
import { queryKeys } from '@/core/queryClient'

export function useAuthCapabilities() {
  return useQuery({
    queryKey: queryKeys.authCapabilities,
    queryFn: ({ signal }) => getAuthCapabilities(signal),
    staleTime: 60_000,
    refetchOnWindowFocus: false,
  })
}
