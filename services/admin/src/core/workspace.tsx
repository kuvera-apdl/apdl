// Workspace model (AD-7): a named connection profile — service base URLs, API
// key (project_id is derived from it), actor identity. Persisted client-side
// only; the console keeps zero server state.
import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'
import { z } from 'zod'

import type { ServiceConnection } from '@/api/http'

export const API_KEY_PATTERN = /^proj_([a-zA-Z0-9]{1,64})_([a-zA-Z0-9]{16,})$/

export const workspaceSchema = z.object({
  id: z.string().min(1),
  name: z.string().min(1),
  ingestionUrl: z.string().url(),
  configUrl: z.string().url(),
  queryUrl: z.string().url(),
  agentsUrl: z.string().url(),
  apiKey: z.string().regex(API_KEY_PATTERN, 'API key must match proj_{project_id}_{secret}'),
  actor: z.string().min(1).max(64),
  internalToken: z.string(),
})

export type Workspace = z.infer<typeof workspaceSchema>

export type ServiceName = 'ingestion' | 'config' | 'query' | 'agents'

const WORKSPACES_KEY = 'apdl-admin:workspaces'
const ACTIVE_KEY = 'apdl-admin:active-workspace'

const env = import.meta.env

export const WORKSPACE_URL_DEFAULTS: Record<`${ServiceName}Url`, string> = {
  ingestionUrl: env.VITE_INGESTION_URL ?? 'http://localhost:8080',
  configUrl: env.VITE_CONFIG_URL ?? 'http://localhost:8081',
  queryUrl: env.VITE_QUERY_URL ?? 'http://localhost:8082',
  agentsUrl: env.VITE_AGENTS_URL ?? 'http://localhost:8083',
}

export function projectIdFromKey(apiKey: string): string | null {
  const match = API_KEY_PATTERN.exec(apiKey)
  return match ? match[1] : null
}

export function serviceBaseUrl(workspace: Workspace, service: ServiceName): string {
  switch (service) {
    case 'ingestion':
      return workspace.ingestionUrl
    case 'config':
      return workspace.configUrl
    case 'query':
      return workspace.queryUrl
    case 'agents':
      return workspace.agentsUrl
  }
}

export function serviceConnection(workspace: Workspace, service: ServiceName): ServiceConnection {
  return {
    baseUrl: serviceBaseUrl(workspace, service),
    apiKey: workspace.apiKey,
    actor: workspace.actor,
  }
}

function loadWorkspaces(): Workspace[] {
  try {
    const raw = localStorage.getItem(WORKSPACES_KEY)
    if (!raw) return []
    const parsed = z.array(workspaceSchema).safeParse(JSON.parse(raw))
    return parsed.success ? parsed.data : []
  } catch {
    return []
  }
}

function loadActiveId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY)
  } catch {
    return null
  }
}

interface WorkspaceContextValue {
  workspaces: Workspace[]
  active: Workspace | null
  projectId: string | null
  /** Insert or update a workspace and make it active. */
  saveWorkspace: (workspace: Workspace) => void
  deleteWorkspace: (id: string) => void
  setActive: (id: string) => void
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const [workspaces, setWorkspaces] = useState<Workspace[]>(loadWorkspaces)
  const [activeId, setActiveId] = useState<string | null>(loadActiveId)

  useEffect(() => {
    localStorage.setItem(WORKSPACES_KEY, JSON.stringify(workspaces))
  }, [workspaces])

  useEffect(() => {
    if (activeId === null) localStorage.removeItem(ACTIVE_KEY)
    else localStorage.setItem(ACTIVE_KEY, activeId)
  }, [activeId])

  const value = useMemo<WorkspaceContextValue>(() => {
    const active = workspaces.find((workspace) => workspace.id === activeId) ?? null
    return {
      workspaces,
      active,
      projectId: active ? projectIdFromKey(active.apiKey) : null,
      saveWorkspace: (workspace) => {
        setWorkspaces((previous) => {
          const index = previous.findIndex((entry) => entry.id === workspace.id)
          if (index === -1) return [...previous, workspace]
          const next = [...previous]
          next[index] = workspace
          return next
        })
        setActiveId(workspace.id)
      },
      deleteWorkspace: (id) => {
        setWorkspaces((previous) => {
          const next = previous.filter((entry) => entry.id !== id)
          setActiveId((current) => (current === id ? (next[0]?.id ?? null) : current))
          return next
        })
      },
      setActive: setActiveId,
    }
  }, [workspaces, activeId])

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext)
  if (!context) throw new Error('useWorkspace must be used within WorkspaceProvider')
  return context
}
