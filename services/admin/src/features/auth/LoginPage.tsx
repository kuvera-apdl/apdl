import { AlertCircle, Loader2, ShieldCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
import { Navigate, useLocation, useNavigate } from 'react-router-dom'
import { z } from 'zod'

import { authenticateAdmin } from '@/api/auth'
import { ApiError, normalizeBaseUrl } from '@/api/http'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Select } from '@/components/ui/select'
import { useAuth } from '@/core/auth'
import {
  API_KEY_PATTERN,
  useWorkspace,
  WORKSPACE_URL_DEFAULTS,
  type Workspace,
} from '@/core/workspace'

const NEW_WORKSPACE = '__new__'
const loginSchema = z.object({
  name: z.string().trim().min(1, 'Workspace name is required'),
  configUrl: z.string().url('Config URL must be valid'),
  apiKey: z
    .string()
    .regex(API_KEY_PATTERN, 'API key must match proj_{project_id}_{secret}'),
})

function loginError(error: unknown): string {
  if (!(error instanceof ApiError)) return 'Unable to sign in. Try again.'
  if (error.status === 401) return 'The API key is invalid, expired, or revoked.'
  if (error.status === 403) return 'This credential cannot access the Config service.'
  if (error.status === 0) return 'Could not reach the Config service.'
  return error.message
}

export function LoginPage() {
  const { workspaces, active, saveWorkspace } = useWorkspace()
  const { authenticated, login, logoutReason } = useAuth()
  const navigate = useNavigate()
  const location = useLocation()
  const returnTo = (location.state as { from?: string } | null)?.from ?? '/'
  const initialWorkspace = active ?? workspaces[0] ?? null

  const [workspaceId, setWorkspaceId] = useState(initialWorkspace?.id ?? NEW_WORKSPACE)
  const [name, setName] = useState(initialWorkspace?.name ?? 'Local')
  const [configUrl, setConfigUrl] = useState(
    initialWorkspace?.configUrl ?? WORKSPACE_URL_DEFAULTS.configUrl,
  )
  const [apiKey, setApiKey] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)

  if (authenticated) return <Navigate to={returnTo} replace />

  const selectWorkspace = (id: string) => {
    setWorkspaceId(id)
    setApiKey('')
    setError(null)
    const workspace = workspaces.find((entry) => entry.id === id)
    setName(workspace?.name ?? 'Local')
    setConfigUrl(workspace?.configUrl ?? WORKSPACE_URL_DEFAULTS.configUrl)
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    setError(null)
    const parsed = loginSchema.safeParse({ name, configUrl, apiKey })
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? 'Invalid login details')
      return
    }

    const values = parsed.data
    const existing = workspaces.find((workspace) => workspace.id === workspaceId)
    const workspace: Workspace = existing
      ? {
          ...existing,
          configUrl: normalizeBaseUrl(values.configUrl),
          apiKey: values.apiKey,
        }
      : {
          id: crypto.randomUUID(),
          name: values.name,
          ...WORKSPACE_URL_DEFAULTS,
          configUrl: normalizeBaseUrl(values.configUrl),
          apiKey: values.apiKey,
          actor: 'admin',
          internalToken: '',
        }

    setSubmitting(true)
    try {
      const identity = await authenticateAdmin(workspace.configUrl, workspace.apiKey)
      saveWorkspace(workspace)
      login(workspace.id, workspace.apiKey, identity)
      navigate(returnTo, { replace: true })
    } catch (caught) {
      setError(loginError(caught))
    } finally {
      setSubmitting(false)
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center bg-muted/30 p-4">
      <div className="w-full max-w-md space-y-4">
        <div className="flex items-center justify-center gap-2 text-lg font-semibold">
          <ShieldCheck className="h-5 w-5" />
          APDL Admin
        </div>
        <Card>
          <CardHeader>
            <CardTitle>Sign in</CardTitle>
            <CardDescription>
              Authenticate with a project-scoped API key. The server determines your project and
              roles from the credential registry.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form className="space-y-4" onSubmit={submit} noValidate>
              {logoutReason === 'unauthorized' ? (
                <div
                  role="alert"
                  className="flex gap-2 rounded-md border border-amber-300 bg-amber-50 p-3 text-sm text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200"
                >
                  <AlertCircle className="mt-0.5 h-4 w-4 shrink-0" />
                  Your session expired or the API key was revoked. Sign in again.
                </div>
              ) : null}

              {workspaces.length > 0 ? (
                <div className="space-y-1.5">
                  <Label htmlFor="login-workspace">Workspace</Label>
                  <Select
                    id="login-workspace"
                    value={workspaceId}
                    onChange={(event) => selectWorkspace(event.target.value)}
                  >
                    {workspaces.map((workspace) => (
                      <option key={workspace.id} value={workspace.id}>
                        {workspace.name}
                      </option>
                    ))}
                    <option value={NEW_WORKSPACE}>New workspace…</option>
                  </Select>
                </div>
              ) : null}

              {workspaceId === NEW_WORKSPACE ? (
                <div className="space-y-1.5">
                  <Label htmlFor="login-name">Workspace name</Label>
                  <Input
                    id="login-name"
                    value={name}
                    onChange={(event) => setName(event.target.value)}
                    autoComplete="organization"
                  />
                </div>
              ) : null}

              <div className="space-y-1.5">
                <Label htmlFor="login-config-url">Config service URL</Label>
                <Input
                  id="login-config-url"
                  value={configUrl}
                  onChange={(event) => setConfigUrl(event.target.value)}
                  inputMode="url"
                />
              </div>

              <div className="space-y-1.5">
                <Label htmlFor="login-api-key">API key</Label>
                <Input
                  id="login-api-key"
                  type="password"
                  value={apiKey}
                  onChange={(event) => setApiKey(event.target.value)}
                  placeholder="proj_demo_…"
                  autoComplete="current-password"
                  autoFocus
                />
              </div>

              {error ? (
                <p role="alert" className="text-sm text-destructive">
                  {error}
                </p>
              ) : null}

              <Button className="w-full" type="submit" disabled={submitting}>
                {submitting ? <Loader2 className="animate-spin" /> : null}
                Sign in
              </Button>
            </form>
          </CardContent>
        </Card>
        <p className="text-center text-xs text-muted-foreground">
          Sessions are scoped to this browser tab. Service APIs still enforce every request.
        </p>
      </div>
    </main>
  )
}
