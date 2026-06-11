// One EventSource per active workspace (AD-5). SSE events are invalidation,
// not trust: admin views need FlagConfig fields the client payload lacks, so
// handlers invalidate TanStack caches and let hooks re-fetch.
import { useQueryClient } from '@tanstack/react-query'
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { toast } from 'sonner'

import { flagUpdatePayloadSchema } from '@/api/schemas/flags'
import { FlagStream, streamUrl, type StreamState } from '@/api/sse'
import { queryKeys } from '@/core/queryClient'
import { useWorkspace } from '@/core/workspace'

const IDLE_STATE: StreamState = { status: 'idle', lastEventAt: null, reconnects: 0 }

interface LiveContextValue {
  state: StreamState
}

const LiveContext = createContext<LiveContextValue>({ state: IDLE_STATE })

export function LiveProvider({ children }: { children: ReactNode }) {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  const [state, setState] = useState<StreamState>(IDLE_STATE)

  const wsId = active?.id ?? null
  const configUrl = active?.configUrl ?? null
  const apiKey = active?.apiKey ?? null

  useEffect(() => {
    if (!wsId || !configUrl || !apiKey || typeof EventSource === 'undefined') {
      setState(IDLE_STATE)
      return
    }
    const stream = new FlagStream(streamUrl(configUrl, apiKey), {
      onState: setState,
      onEvent: (name, data) => {
        if (name === 'flag_update') {
          void queryClient.invalidateQueries({ queryKey: queryKeys.flagsPrefix(wsId) })
          void queryClient.invalidateQueries({ queryKey: queryKeys.staleFlagsPrefix(wsId) })
          const payload = flagUpdatePayloadSchema.safeParse(data)
          if (payload.success) {
            const key = 'flag' in payload.data ? payload.data.flag.key : payload.data.key
            const action = payload.data.action.replace('flag_', '')
            toast.message(`Flag "${key}" ${action}`, {
              description: 'Changed outside this console — views refreshed.',
            })
          } else {
            toast.message('Flag configuration changed', { description: 'Views refreshed.' })
          }
        }
      },
    })
    stream.start()
    return () => stream.stop()
  }, [wsId, configUrl, apiKey, queryClient])

  const value = useMemo(() => ({ state }), [state])
  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>
}

export function useLive(): LiveContextValue {
  return useContext(LiveContext)
}
