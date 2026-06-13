import { useMutation, useQueryClient } from '@tanstack/react-query'

import { archiveFlag, cleanupFlag, createFlag, disableFlag, updateFlag } from '@/api/config'
import type { FlagCleanup, FlagCreate, FlagDisable, FlagUpdate } from '@/api/types/flags'
import { noteLocalWrite } from '@/core/localWrites'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'

function useOnFlagWritten() {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  return (key: string) => {
    noteLocalWrite(key)
    if (!active) return
    void queryClient.invalidateQueries({ queryKey: queryKeys.flagsPrefix(active.id) })
    void queryClient.invalidateQueries({ queryKey: queryKeys.staleFlagsPrefix(active.id) })
  }
}

export function useCreateFlagMutation() {
  const { active } = useWorkspace()
  const onWritten = useOnFlagWritten()
  return useMutation({
    mutationFn: (body: FlagCreate) => createFlag(serviceConnection(active!, 'config'), body),
    onSuccess: (response) => onWritten(response.flag.key),
  })
}

export function useUpdateFlagMutation(key: string) {
  const { active } = useWorkspace()
  const onWritten = useOnFlagWritten()
  return useMutation({
    mutationFn: (body: FlagUpdate) => updateFlag(serviceConnection(active!, 'config'), key, body),
    onSuccess: () => onWritten(key),
  })
}

export function useDisableFlagMutation(key: string) {
  const { active } = useWorkspace()
  const onWritten = useOnFlagWritten()
  return useMutation({
    mutationFn: (body: FlagDisable) => disableFlag(serviceConnection(active!, 'config'), key, body),
    onSuccess: () => onWritten(key),
  })
}

export function useArchiveFlagMutation(key: string) {
  const { active } = useWorkspace()
  const onWritten = useOnFlagWritten()
  return useMutation({
    mutationFn: () => archiveFlag(serviceConnection(active!, 'config'), key),
    onSuccess: () => onWritten(key),
  })
}

export function useCleanupFlagMutation(key: string) {
  const { active } = useWorkspace()
  const onWritten = useOnFlagWritten()
  return useMutation({
    mutationFn: (body: FlagCleanup) => cleanupFlag(serviceConnection(active!, 'config'), key, body),
    onSuccess: () => onWritten(key),
  })
}
