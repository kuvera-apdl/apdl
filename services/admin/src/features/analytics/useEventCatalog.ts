// Event discovery (gap G4): POST /v1/query/events/names returns every event
// name a project has sent in a window, ranked by volume — regular and custom
// alike. Powers the event pickers so they list events that actually exist
// instead of a hardcoded guess.
import { eventNames } from '@/api/query'
import { useWorkspace } from '@/core/workspace'

import { lastDays } from './selectorModel'
import { useAnalyticsQuery } from './useAnalyticsQuery'

// Wide lookback + max limit so the picker shows effectively all of a project's
// events, independent of whatever analysis window a page is currently using.
const CATALOG_LOOKBACK_DAYS = 365
const CATALOG_LIMIT = 1000

export function useEventCatalog() {
  const { projectId } = useWorkspace()
  const body = projectId
    ? { project_id: projectId, ...lastDays(CATALOG_LOOKBACK_DAYS), limit: CATALOG_LIMIT }
    : null
  const query = useAnalyticsQuery('event-catalog', body, eventNames)
  const names = (query.data?.events ?? []).map((event) => event.event_name)
  return { names, isPending: query.isPending, error: query.error }
}
