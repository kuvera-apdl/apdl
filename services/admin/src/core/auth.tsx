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

import {
  createAdminProject,
  getAdminSession,
  loginAdmin,
  logoutAdmin,
  registerAdmin,
  type AuthIdentity,
} from '@/api/auth'
import { ApiError } from '@/api/http'
import {
  acceptProjectInvitation,
  registerWithProjectInvitation,
} from '@/api/members'
import {
  AUTH_UNAUTHORIZED_EVENT,
  PROJECT_ACCESS_REVOKED_EVENT,
} from '@/core/auth-events'

type LogoutReason = 'unauthorized' | null

interface AuthContextValue {
  authenticated: boolean
  initializing: boolean
  identity: AuthIdentity | null
  logoutReason: LogoutReason
  login: (email: string, password: string) => Promise<void>
  register: (email: string, password: string) => Promise<void>
  acceptInvitation: (token: string) => Promise<void>
  registerInvitation: (token: string, password: string) => Promise<void>
  createProject: (projectId: string) => Promise<void>
  refreshIdentity: () => Promise<void>
  logout: () => Promise<void>
}

const AuthContext = createContext<AuthContextValue | null>(null)

function purgeLegacyCredentials(): void {
  try {
    localStorage.removeItem('apdl-admin:workspaces')
    sessionStorage.removeItem('apdl-admin:session')
  } catch {
    // Storage can be unavailable in privacy-restricted browsing contexts.
  }
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const queryClient = useQueryClient()
  const [identity, setIdentity] = useState<AuthIdentity | null>(null)
  const [initializing, setInitializing] = useState(true)
  const [logoutReason, setLogoutReason] = useState<LogoutReason>(null)

  const endSession = useCallback(
    (reason: LogoutReason) => {
      setIdentity(null)
      setLogoutReason(reason)
      queryClient.clear()
    },
    [queryClient],
  )

  const refreshIdentity = useCallback(async () => {
    const session = await getAdminSession()
    queryClient.clear()
    setIdentity(session)
    setLogoutReason(null)
  }, [queryClient])

  useEffect(() => {
    purgeLegacyCredentials()
    let active = true
    void getAdminSession()
      .then((session) => {
        if (active) setIdentity(session)
      })
      .catch(() => {
        if (active) setIdentity(null)
      })
      .finally(() => {
        if (active) setInitializing(false)
      })
    return () => {
      active = false
    }
  }, [])

  useEffect(() => {
    const handleUnauthorized = () => endSession('unauthorized')
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized)
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, handleUnauthorized)
  }, [endSession])

  useEffect(() => {
    let active = true
    const refreshProjectAccess = () => {
      void refreshIdentity()
        .then(() => undefined)
        .catch(() => {
          if (active) endSession('unauthorized')
        })
    }
    window.addEventListener(PROJECT_ACCESS_REVOKED_EVENT, refreshProjectAccess)
    return () => {
      active = false
      window.removeEventListener(PROJECT_ACCESS_REVOKED_EVENT, refreshProjectAccess)
    }
  }, [endSession, refreshIdentity])

  const value = useMemo<AuthContextValue>(
    () => ({
      authenticated: identity !== null,
      initializing,
      identity,
      logoutReason,
      login: async (email, password) => {
        const next = await loginAdmin(email, password)
        queryClient.clear()
        setIdentity(next)
        setLogoutReason(null)
      },
      register: async (email, password) => {
        const next = await registerAdmin(email, password)
        queryClient.clear()
        setIdentity(next)
        setLogoutReason(null)
      },
      acceptInvitation: async (token) => {
        const next = await acceptProjectInvitation(token)
        queryClient.clear()
        setIdentity(next)
        setLogoutReason(null)
      },
      registerInvitation: async (token, password) => {
        const next = await registerWithProjectInvitation(token, password)
        queryClient.clear()
        setIdentity(next)
        setLogoutReason(null)
      },
      createProject: async (projectId) => {
        const next = await createAdminProject(projectId)
        queryClient.clear()
        setIdentity(next)
      },
      refreshIdentity,
      logout: async () => {
        try {
          await logoutAdmin()
        } catch (error) {
          if (!(error instanceof ApiError) || error.status !== 401) throw error
          endSession(null)
          return
        }
        endSession(null)
      },
    }),
    [
      endSession,
      identity,
      initializing,
      logoutReason,
      queryClient,
      refreshIdentity,
    ],
  )

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext)
  if (!context) throw new Error('useAuth must be used within AuthProvider')
  return context
}

export function useOptionalAuth(): AuthContextValue | null {
  return useContext(AuthContext)
}
