// One EventSource per active workspace (AD-5). SSE events are invalidation,
// not trust: admin views need FlagConfig fields the client payload lacks, so
// handlers invalidate TanStack caches and let hooks re-fetch. The one
// exception (per plan §6) is the tester's "served config" panel, which renders
// the SSE payload directly — exactly what SDKs see — so the provider also
// maintains the latest client flag collection.
import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'

import { flagCollectionSchema, flagUpdatePayloadSchema } from '@/api/schemas/flags'
import type { ClientFlagConfig } from '@/api/types/flags'
import { FlagStream, streamUrl, type StreamState } from '@/api/sse'
import {
  AUTH_UNAUTHORIZED_EVENT,
  PROJECT_ACCESS_REVOKED_EVENT,
} from '@/core/auth-events'
import { useAuth } from '@/core/auth'
import { wasRecentlyWrittenLocally } from '@/core/localWrites'
import { queryKeys } from '@/core/queryClient'
import { useWorkspace } from '@/core/workspace'
import { serviceBaseUrl } from '@/core/workspace'

const IDLE_STATE: StreamState = { status: 'idle', lastEventAt: null, reconnects: 0 }

interface LiveContextValue {
  state: StreamState
  /** Latest SDK-visible flag payloads from the stream; null before the first config event. */
  servedFlags: Map<string, ClientFlagConfig> | null
}

const LiveContext = createContext<LiveContextValue>({ state: IDLE_STATE, servedFlags: null })

export function LiveProvider({ children }: { children: ReactNode }) {
  const { active } = useWorkspace()
  const { authenticated } = useAuth()
  const queryClient = useQueryClient()
  const [state, setState] = useState<StreamState>(IDLE_STATE)
  const [servedFlags, setServedFlags] = useState<Map<string, ClientFlagConfig> | null>(null)

  const wsId = authenticated ? (active?.id ?? null) : null
  const configUrl = authenticated && active ? serviceBaseUrl(active, 'config') : null
  const canReadConfig = active?.roles.includes('config:read') ?? false

  useEffect(() => {
    setServedFlags(null)
    if (!wsId || !configUrl || !canReadConfig || typeof EventSource === 'undefined') {
      setState(IDLE_STATE)
      return
    }
    const stream = new FlagStream(streamUrl(configUrl), {
      onState: setState,
      onEvent: (name, data) => {
        if (name === 'auth_expired') {
          stream.stop()
          window.dispatchEvent(new Event(AUTH_UNAUTHORIZED_EVENT))
          return
        }
        if (name === 'project_access_revoked') {
          stream.stop()
          window.dispatchEvent(new Event(PROJECT_ACCESS_REVOKED_EVENT))
          return
        }
        if (name === 'config') {
          // The versioned config event is the project-wide synchronization
          // barrier. Flags arrive inline; experiment state is not part of the
          // SDK schema, so reconcile it with an authoritative refetch.
          void queryClient.invalidateQueries({ queryKey: queryKeys.experiments(wsId) })
          const collection = flagCollectionSchema.safeParse(data)
          if (collection.success) {
            setServedFlags(new Map(collection.data.flags.map((flag) => [flag.key, flag])))
          }
          return
        }
        if (name === 'flag_update') {
          void queryClient.invalidateQueries({ queryKey: queryKeys.flagsPrefix(wsId) })
          void queryClient.invalidateQueries({ queryKey: queryKeys.staleFlagsPrefix(wsId) })
          const payload = flagUpdatePayloadSchema.safeParse(data)
          if (payload.success) {
            const parsed = payload.data
            setServedFlags((previous) => {
              const next = new Map(previous ?? [])
              if ('flag' in parsed) next.set(parsed.flag.key, parsed.flag)
              else next.delete(parsed.key)
              return next
            })
            const key = 'flag' in parsed ? parsed.flag.key : parsed.key
            if (!wasRecentlyWrittenLocally(key)) {
              const action = parsed.action.replace('flag_', '')
              toast.message(`Flag "${key}" ${action}`, {
                description: 'Changed outside this console — views refreshed.',
              })
            }
          } else {
            toast.message('Flag configuration changed', { description: 'Views refreshed.' })
          }
          return
        }
        if (name === 'experiment_update') {
          void queryClient.invalidateQueries({ queryKey: queryKeys.experiments(wsId) })
        }
      },
    })
    stream.start()
    return () => stream.stop()
  }, [wsId, configUrl, canReadConfig, queryClient])

  const value = useMemo(() => ({ state, servedFlags }), [state, servedFlags])
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext)
}
