import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'

import type { ServiceConnection } from '@/api/http'
import type { AdminRole, AuthIdentity } from '@/api/auth'
import { useOptionalAuth } from '@/core/auth'

export type ServiceName =
  | 'ingestion'
  | 'config'
  | 'query'
  | 'agents'
  | 'codegen'
  | 'llm-vault'

export interface Workspace {
  id: string
  name: string
  projectId: string
  actor: string
  roles: AdminRole[]
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

export function hasWorkspaceRole(
  workspace: Workspace | null | undefined,
  role: AdminRole,
): boolean {
  return workspace?.roles.includes(role) ?? false
}

export function workspacesForIdentity(identity: AuthIdentity | null): Workspace[] {
  return (
    identity?.projects.map(({ project_id, roles }) => ({
      id: project_id,
      name: project_id,
      projectId: project_id,
      actor: identity.email,
      roles: [...roles],
    })) ?? []
  )
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
  const pendingActiveId = useRef<string | null>(null)
  const workspaces = useMemo<Workspace[]>(
    () => initialWorkspaces ?? workspacesForIdentity(identity),
    [identity, initialWorkspaces],
  )
  const active = workspaces.find((workspace) => workspace.id === activeId) ?? workspaces[0] ?? null

  useEffect(() => {
    // AuthProvider populates the workspace list asynchronously on a full-page
    // load. Keep the persisted selection until that initial request settles;
    // otherwise every redirect briefly has zero workspaces and is normalized
    // to the first project in the eventual identity.
    if (initialWorkspaces === undefined && auth?.initializing) return
    // A child can request a workspace change before this effect from the
    // previous render runs. Do not let that stale normalization overwrite the
    // newer explicit selection.
    if (pendingActiveId.current !== null && pendingActiveId.current !== activeId) return
    const candidateExists = workspaces.some((workspace) => workspace.id === activeId)
    if (pendingActiveId.current === activeId && !candidateExists) return
    if (candidateExists) pendingActiveId.current = null
    try {
      if (active === null) localStorage.removeItem(ACTIVE_KEY)
      else localStorage.setItem(ACTIVE_KEY, active.id)
    } catch {
      // The active project remains usable in memory when storage is unavailable.
    }
    if (active?.id !== activeId) setActiveId(active?.id ?? null)
  }, [active, activeId, auth?.initializing, initialWorkspaces, workspaces])

  const value = useMemo<WorkspaceContextValue>(
    () => ({
      workspaces,
      active,
      projectId: active?.projectId ?? null,
      setActive: (id) => {
        // Invitation acceptance updates AuthProvider and this provider in the
        // same React turn. Preserve the strict candidate until the new
        // identity supplies its workspace instead of falling back to the old
        // active project.
        if (/^[A-Za-z0-9]{1,64}$/.test(id)) {
          pendingActiveId.current = id
          setActiveId(id)
        }
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
