import { useQueryClient } from '@tanstack/react-query'
import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from 'react'
import { z } from 'zod'

import { authIdentitySchema, type AuthIdentity } from '@/api/auth'
import { AUTH_UNAUTHORIZED_EVENT } from '@/core/auth-events'
import { useWorkspace } from '@/core/workspace'

export const AUTH_SESSION_KEY = 'apdl-admin:session'

const authSessionSchema = z
  .object({
    workspaceId: z.string().min(1),
    apiKey: z.string().min(1),
    identity: authIdentitySchema,
  })
  .strict()

type AuthSession = z.infer<typeof authSessionSchema>
type LogoutReason = 'unauthorized' | null

function loadSession(): AuthSession | null {
  try {
    const raw = sessionStorage.getItem(AUTH_SESSION_KEY)
    if (!raw) return null
    const parsed = authSessionSchema.safeParse(JSON.parse(raw))
    return parsed.success ? parsed.data : null
  } catch {
    return null
  }
}

function storeSession(session: AuthSession | null): void {
  try {
    if (session === null) sessionStorage.removeItem(AUTH_SESSION_KEY)
    else sessionStorage.setItem(AUTH_SESSION_KEY, JSON.stringify(session))
  } catch {
    // A tab can still use the in-memory session when storage is unavailable.
  }
}

interface AuthContextValue {
  authenticated: boolean
  identity: AuthIdentity | null
  logoutReason: LogoutReason
  login: (workspaceId: string, apiKey: string, identity: AuthIdentity) => void
  logout: () => void
}

const AuthContext = createContext<AuthContextValue | null>(null)

export function AuthProvider({ children }: { children: ReactNode }) {
  const { active, workspaces } = useWorkspace()
  const queryClient = useQueryClient()
  const [session, setSession] = useState<AuthSession | null>(loadSession)
  const [logoutReason, setLogoutReason] = useState<LogoutReason>(null)

  const endSession = useCallback(
    (reason: LogoutReason) => {
      storeSession(null)
      setSession(null)
      setLogoutReason(reason)
      queryClient.clear()
    },
    [queryClient],
  )

  useEffect(() => {
    const handleUnauthorized = () => endSession('unauthorized')
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized)
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized)
  }, [endSession])

  useEffect(() => {
    if (session && !workspaces.some((workspace) => workspace.id === session.workspaceId)) {
      endSession(null)
    }
  }, [endSession, session, workspaces])

  const authenticated =
    session !== null && active?.id === session.workspaceId && active.apiKey === session.apiKey
  const value = useMemo<AuthContextValue>(
    () => ({
      authenticated,
      identity: authenticated ? session.identity : null,
      logoutReason,
      login: (workspaceId, apiKey, identity) => {
        const next = { workspaceId, apiKey, identity }
        queryClient.clear()
        storeSession(next)
        setSession(next)
        setLogoutReason(null)
      },
      logout: () => endSession(null),
    }),
    [authenticated, endSession, logoutReason, queryClient, session],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}
