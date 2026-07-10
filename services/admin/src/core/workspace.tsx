import { createContext, useContext, useEffect, useMemo, useState, type ReactNode } from 'react'

import type { ServiceConnection } from '@/api/http'
import { useOptionalAuth } from '@/core/auth'

export type ServiceName = 'ingestion' | 'config' | 'query' | 'agents' | 'codegen'

export interface Workspace {
  id: string
  name: string
  projectId: string
  actor: string
}

const ACTIVE_KEY = 'apdl-admin:active-project'

function loadActiveId(): string | null {
  try {
    return localStorage.getItem(ACTIVE_KEY)
  } catch {
    return null
  }
}

export function serviceBaseUrl(workspace: Workspace, service: ServiceName): string {
  return `/api/projects/${encodeURIComponent(workspace.projectId)}/${service}`
}

export function serviceConnection(workspace: Workspace, service: ServiceName): ServiceConnection {
  return {
    baseUrl: serviceBaseUrl(workspace, service),
    actor: workspace.actor,
  }
}

interface WorkspaceContextValue {
  workspaces: Workspace[]
  active: Workspace | null
  projectId: string | null
  setActive: (id: string) => void
}

const WorkspaceContext = createContext<WorkspaceContextValue | null>(null)

export function WorkspaceProvider({
  children,
  initialWorkspaces,
}: {
  children: ReactNode
  /** Explicit dependency-injection seam for isolated component tests. */
  initialWorkspaces?: Workspace[]
}) {
  const auth = useOptionalAuth()
  if (auth === null && initialWorkspaces === undefined) {
    throw new Error('WorkspaceProvider requires AuthProvider')
  }
  const identity = auth?.identity ?? null
  const [activeId, setActiveId] = useState<string | null>(loadActiveId)
  const workspaces = useMemo<Workspace[]>(
    () =>
      initialWorkspaces ?? identity?.projects.map(({ project_id }) => ({
        id: project_id,
        name: project_id,
        projectId: project_id,
        actor: identity.email,
      })) ?? [],
    [identity, initialWorkspaces],
  )
  const active = workspaces.find((workspace) => workspace.id === activeId) ?? workspaces[0] ?? null

  useEffect(() => {
    try {
      if (active === null) localStorage.removeItem(ACTIVE_KEY)
      else localStorage.setItem(ACTIVE_KEY, active.id)
    } catch {
      // The active project remains usable in memory when storage is unavailable.
    }
    if (active?.id !== activeId) setActiveId(active?.id ?? null)
  }, [active, activeId])

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspaces,
      active,
      projectId: active?.projectId ?? null,
      setActive: (id) => {
        if (workspaces.some((workspace) => workspace.id === id)) setActiveId(id)
      },
    }),
    [active, workspaces],
  )

  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>
}

export function useWorkspace(): WorkspaceContextValue {
  const context = useContext(WorkspaceContext)
  if (!context) throw new Error('useWorkspace must be used within WorkspaceProvider')
  return context
}
