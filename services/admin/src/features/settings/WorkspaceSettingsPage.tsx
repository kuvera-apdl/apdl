import { zodResolver } from '@hookform/resolvers/zod'
import { CheckCircle2, Loader2, XCircle } from 'lucide-react'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { checkService, healthLevel, SERVICE_DESCRIPTORS, type ServiceHealth } from '@/api/health'
import { normalizeBaseUrl } from '@/api/http'
import { JsonView } from '@/components/shared/JsonView'
import { PageHeader } from '@/components/shared/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import {
  API_KEY_PATTERN,
  projectIdFromKey,
  serviceBaseUrl,
  useWorkspace,
  WORKSPACE_URL_DEFAULTS,
  type ServiceName,
  type Workspace,
} from '@/core/workspace'
import { formatMs } from '@/lib/format'

const workspaceFormSchema = z.object({
  name: z.string().min(1, 'Required'),
  ingestionUrl: z.string().url('Must be a valid URL'),
  configUrl: z.string().url('Must be a valid URL'),
  queryUrl: z.string().url('Must be a valid URL'),
  agentsUrl: z.string().url('Must be a valid URL'),
  apiKey: z
    .string()
    .regex(API_KEY_PATTERN, 'Format: proj_{project_id}_{secret} (secret ≥ 16 alphanumerics)'),
  actor: z.string().min(1, 'Required').max(64, 'At most 64 characters'),
  internalToken: z.string(),
})

type WorkspaceFormValues = z.infer<typeof workspaceFormSchema>

const FORM_DEFAULTS: WorkspaceFormValues = {
  name: 'Local',
  ...WORKSPACE_URL_DEFAULTS,
  apiKey: '',
  actor: 'admin',
  internalToken: '',
}

function valuesFromWorkspace(workspace: Workspace): WorkspaceFormValues {
  const { id: _id, ...values } = workspace
  return values
}

type TestResults = Partial<Record<ServiceName, ServiceHealth | 'pending'>>

interface FieldProps {
  label: string
  error?: string
  hint?: string
  children: React.ReactNode
}

function Field({ label, error, hint, children }: FieldProps) {
  return (
    <div className="space-y-1.5">
      <Label>{label}</Label>
      {children}
      {error ? <p className="text-xs text-destructive">{error}</p> : null}
      {!error && hint ? <p className="text-xs text-muted-foreground">{hint}</p> : null}
    </div>
  )
}

export function WorkspaceSettingsPage() {
  const { workspaces, active, saveWorkspace, deleteWorkspace, setActive } = useWorkspace()
  const [editingId, setEditingId] = useState<string | null>(null)
  const [testResults, setTestResults] = useState<TestResults>({})
  const [testing, setTesting] = useState(false)
  const [deleteTarget, setDeleteTarget] = useState<Workspace | null>(null)

  const form = useForm<WorkspaceFormValues>({
    resolver: zodResolver(workspaceFormSchema),
    defaultValues: FORM_DEFAULTS,
  })
  const { register, handleSubmit, watch, trigger, getValues, reset, formState } = form

  const apiKey = watch('apiKey')
  const derivedProjectId = projectIdFromKey(apiKey)

  const mixedContent =
    window.location.protocol === 'https:' &&
    (['ingestionUrl', 'configUrl', 'queryUrl', 'agentsUrl'] as const).some((field) =>
      (watch(field) ?? '').startsWith('http://'),
    )

  const startEditing = (workspace: Workspace) => {
    setEditingId(workspace.id)
    setTestResults({})
    reset(valuesFromWorkspace(workspace))
  }

  const startCreating = () => {
    setEditingId(null)
    setTestResults({})
    reset(FORM_DEFAULTS)
  }

  const normalize = (values: WorkspaceFormValues): WorkspaceFormValues => ({
    ...values,
    ingestionUrl: normalizeBaseUrl(values.ingestionUrl),
    configUrl: normalizeBaseUrl(values.configUrl),
    queryUrl: normalizeBaseUrl(values.queryUrl),
    agentsUrl: normalizeBaseUrl(values.agentsUrl),
  })

  const onSubmit = (values: WorkspaceFormValues) => {
    const workspace: Workspace = {
      id: editingId ?? crypto.randomUUID(),
      ...normalize(values),
    }
    saveWorkspace(workspace)
    setEditingId(workspace.id)
    toast.success(`Workspace "${workspace.name}" saved and activated`)
  }

  const testConnection = async () => {
    const valid = await trigger(['ingestionUrl', 'configUrl', 'queryUrl', 'agentsUrl', 'apiKey'])
    if (!valid) return
    const values = normalize(getValues())
    const probeTarget: Workspace = { id: 'probe', ...values }
    setTesting(true)
    setTestResults({
      ingestion: 'pending',
      config: 'pending',
      query: 'pending',
      agents: 'pending',
    })
    await Promise.all(
      SERVICE_DESCRIPTORS.map(async ({ service }) => {
        const result = await checkService({
          service,
          baseUrl: serviceBaseUrl(probeTarget, service),
          apiKey: values.apiKey,
        })
        setTestResults((previous) => ({ ...previous, [service]: result }))
      }),
    )
    setTesting(false)
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Workspace"
        description="Everything the console needs to talk to an APDL stack. Stored locally in this browser — the console keeps no server state."
      />

      {workspaces.length === 0 ? (
        <Card className="border-amber-300 bg-amber-50 dark:border-amber-900 dark:bg-amber-950/30">
          <CardContent className="p-4 text-sm">
            Welcome — configure a workspace to start. The defaults match a local{' '}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">dev.sh up-full</code> stack.
          </CardContent>
        </Card>
      ) : null}

      <div className="grid items-start gap-6 lg:grid-cols-[1fr_minmax(280px,380px)]">
        <Card>
          <CardHeader>
            <CardTitle>{editingId ? 'Edit workspace' : 'New workspace'}</CardTitle>
            <CardDescription>
              Services verify the API key against PostgreSQL and derive its project and roles from
              the stored credential. Switching workspaces requires signing in again.
            </CardDescription>
          </CardHeader>
          <CardContent>
            <form onSubmit={handleSubmit(onSubmit)} className="space-y-4" noValidate>
              <Field label="Workspace name" error={formState.errors.name?.message}>
                <Input {...register('name')} placeholder="Local" />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field label="Ingestion URL" error={formState.errors.ingestionUrl?.message}>
                  <Input {...register('ingestionUrl')} placeholder="http://localhost:8080" />
                </Field>
                <Field label="Config URL" error={formState.errors.configUrl?.message}>
                  <Input {...register('configUrl')} placeholder="http://localhost:8081" />
                </Field>
                <Field label="Query URL" error={formState.errors.queryUrl?.message}>
                  <Input {...register('queryUrl')} placeholder="http://localhost:8082" />
                </Field>
                <Field label="Agents URL" error={formState.errors.agentsUrl?.message}>
                  <Input {...register('agentsUrl')} placeholder="http://localhost:8083" />
                </Field>
              </div>
              {mixedContent ? (
                <p className="rounded-md border border-amber-300 bg-amber-50 p-2 text-xs text-amber-900 dark:border-amber-900 dark:bg-amber-950/30 dark:text-amber-200">
                  This console is served over HTTPS but the service URLs use HTTP — browsers block
                  such mixed-content requests. Serve the services behind HTTPS or run the console
                  over HTTP.
                </p>
              ) : null}
              <Field
                label="API key"
                error={formState.errors.apiKey?.message}
                hint={
                  derivedProjectId
                    ? `Derived project_id: ${derivedProjectId}`
                    : 'proj_{project_id}_{secret} — project_id is derived from the key'
                }
              >
                <Input {...register('apiKey')} placeholder="proj_demo_0123456789abcdef" className="font-mono" />
              </Field>
              <div className="grid gap-4 sm:grid-cols-2">
                <Field
                  label="Actor name"
                  error={formState.errors.actor?.message}
                  hint="Local display name only. Audit trails use the authenticated credential ID."
                >
                  <Input {...register('actor')} placeholder="your-name" />
                </Field>
                <Field
                  label="Internal token (optional)"
                  error={formState.errors.internalToken?.message}
                  hint="Server secret — only used for opt-in server-side evaluation in the flag tester (later phase)."
                >
                  <Input {...register('internalToken')} type="password" autoComplete="off" />
                </Field>
              </div>
              <div className="flex flex-wrap items-center gap-2 pt-2">
                <Button type="submit">{editingId ? 'Save changes' : 'Save & activate'}</Button>
                <Button type="button" variant="outline" onClick={testConnection} disabled={testing}>
                  {testing ? <Loader2 className="animate-spin" /> : null}
                  Test connection
                </Button>
                {editingId ? (
                  <Button type="button" variant="ghost" onClick={startCreating}>
                    New workspace instead
                  </Button>
                ) : null}
              </div>
            </form>

            {Object.keys(testResults).length > 0 ? (
              <div className="mt-5 space-y-2">
                {SERVICE_DESCRIPTORS.map(({ service, label }) => {
                  const result = testResults[service]
                  if (!result) return null
                  if (result === 'pending') {
                    return (
                      <div key={service} className="flex items-center gap-2 rounded-md border p-2.5 text-sm">
                        <Loader2 className="h-4 w-4 animate-spin text-muted-foreground" />
                        <span className="w-24 font-medium">{label}</span>
                        <span className="text-muted-foreground">probing…</span>
                      </div>
                    )
                  }
                  const level = healthLevel(result)
                  return (
                    <details key={service} className="rounded-md border p-2.5 text-sm">
                      <summary className="flex cursor-pointer list-none items-center gap-2">
                        {level === 'ok' ? (
                          <CheckCircle2 className="h-4 w-4 text-emerald-600" />
                        ) : (
                          <XCircle className="h-4 w-4 text-destructive" />
                        )}
                        <span className="w-24 font-medium">{label}</span>
                        <span className="text-muted-foreground">
                          {result.health.status !== null
                            ? `HTTP ${result.health.status} · ${formatMs(result.health.latencyMs)}`
                            : (result.health.error ?? 'unreachable')}
                        </span>
                      </summary>
                      {result.health.body !== null ? (
                        <JsonView data={result.health.body} className="mt-2 max-h-40" />
                      ) : null}
                      {result.ready ? (
                        <p className="mt-2 text-xs text-muted-foreground">
                          /ready: {JSON.stringify(result.ready.body)}
                        </p>
                      ) : null}
                    </details>
                  )
                })}
              </div>
            ) : null}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle>Saved workspaces</CardTitle>
            <CardDescription>Switch between stacks (e.g. local vs staging).</CardDescription>
          </CardHeader>
          <CardContent className="space-y-3">
            {workspaces.length === 0 ? (
              <p className="text-sm text-muted-foreground">None yet.</p>
            ) : (
              workspaces.map((workspace) => (
                <div key={workspace.id} className="space-y-2 rounded-md border p-3">
                  <div className="flex items-center justify-between gap-2">
                    <span className="font-medium">{workspace.name}</span>
                    {workspace.id === active?.id ? <Badge>active</Badge> : null}
                  </div>
                  <p className="font-mono text-xs text-muted-foreground">
                    {projectIdFromKey(workspace.apiKey) ?? '?'} · {workspace.configUrl}
                  </p>
                  <div className="flex gap-2">
                    {workspace.id !== active?.id ? (
                      <Button size="sm" variant="outline" onClick={() => setActive(workspace.id)}>
                        Activate
                      </Button>
                    ) : null}
                    <Button size="sm" variant="ghost" onClick={() => startEditing(workspace)}>
                      Edit
                    </Button>
                    <Button
                      size="sm"
                      variant="ghost"
                      className="text-destructive hover:text-destructive"
                      onClick={() => setDeleteTarget(workspace)}
                    >
                      Delete
                    </Button>
                  </div>
                </div>
              ))
            )}
          </CardContent>
        </Card>
      </div>

      <Dialog open={deleteTarget !== null} onOpenChange={(open) => !open && setDeleteTarget(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Delete workspace?</DialogTitle>
            <DialogDescription>
              Removes "{deleteTarget?.name}" and its stored API key from this browser. The APDL
              stack itself is not touched.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setDeleteTarget(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                if (deleteTarget) {
                  deleteWorkspace(deleteTarget.id)
                  if (editingId === deleteTarget.id) startCreating()
                  toast.success(`Workspace "${deleteTarget.name}" deleted`)
                }
                setDeleteTarget(null)
              }}
            >
              Delete
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
