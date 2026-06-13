import { useQuery } from '@tanstack/react-query'

import type { ServiceConnection } from '@/api/http'
import { serviceConnection, useWorkspace } from '@/core/workspace'

/**
 * Run-on-submit analytics query: pages keep editable form state and only set
 * `body` when the user runs the query. Analytics results are cached 60s
 * (heavier queries; plan §4.3).
 */
export function useAnalyticsQuery<TBody, TResult>(
  screen: string,
  body: TBody | null,
  queryFn: (conn: ServiceConnection, body: TBody) => Promise<TResult>,
) {
  const { active } = useWorkspace()
  return useQuery({
    queryKey: [active?.id ?? 'none', 'analytics', screen, JSON.stringify(body)],
    enabled: active !== null && body !== null,
    staleTime: 60_000,
    queryFn: () => queryFn(serviceConnection(active!, 'query'), body!),
  })
}
