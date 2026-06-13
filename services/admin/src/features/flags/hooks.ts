import { useQuery } from '@tanstack/react-query'

import { flagAudit, listFlags, staleFlags } from '@/api/config'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'

// One list query, always include_archived=true — list views filter
// client-side and detail views derive from the same cache (the API has no
// single-flag GET).
export function useFlagsQuery() {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: queryKeys.flags(active?.id ?? 'none'),
    enabled: active !== null,
    queryFn: ({ signal }) =>
      listFlags(serviceConnection(active!, 'config'), { includeArchived: true, signal }),
  })
}

export function useFlagAuditQuery(key: string, limit: number) {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: queryKeys.flagAudit(active?.id ?? 'none', key, limit),
    enabled: active !== null && key.length > 0,
    queryFn: ({ signal }) => flagAudit(serviceConnection(active!, 'config'), key, { limit, signal }),
  })
}

export function useStaleFlagsQuery(olderThanDays: number) {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: queryKeys.staleFlags(active?.id ?? 'none', olderThanDays),
    enabled: active !== null,
    queryFn: ({ signal }) =>
      staleFlags(serviceConnection(active!, 'config'), { olderThanDays, signal }),
  })
}
