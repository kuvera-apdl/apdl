import { useQuery } from '@tanstack/react-query'
import { Eye, KeyRound, Loader2, Plus, RefreshCw, ShieldAlert, Trash2 } from 'lucide-react'
import { useEffect, useRef, useState, type FormEvent } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/http'
import {
  createLlmConnection,
  getLlmConnection,
  listLlmConnections,
  refreshLlmConnection,
  replaceLlmConnection,
  revokeLlmConnection,
  type LlmConnectionSummary,
  type LlmConsumer,
  type LlmProvider,
} from '@/api/llmConnections'
import { getProjectAuthorization } from '@/api/members'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
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
import { useAuth } from '@/core/auth'
import { queryKeys } from '@/core/queryClient'
import { hasWorkspaceRole, serviceConnection, useWorkspace } from '@/core/workspace'

const PROVIDERS = [
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'openai', label: 'OpenAI' },
  { id: 'google', label: 'Google' },
  { id: 'xai', label: 'xAI' },
] as const satisfies readonly { id: LlmProvider; label: string }[]

function providerLabel(provider: LlmProvider): string {
  return PROVIDERS.find((item) => item.id === provider)?.label ?? provider
}

function errorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (typeof error.body === 'object' && error.body !== null && 'detail' in error.body) {
      const detail = error.body.detail
      if (typeof detail === 'object' && detail !== null && 'message' in detail) {
        if (typeof detail.message === 'string') return detail.message
      }
    }
    if (error.status === 403) {
      return 'Changes require project ownership or both agents:manage and credentials:manage.'
    }
    if (error.status === 404 || error.status === 409) return error.message
    if ([502, 503, 504].includes(error.status)) {
      return 'The vault or provider validation service is unavailable. Nothing was changed.'
    }
  }
  return 'The LLM connection could not be changed. Try again shortly.'
}

function canonicalConsumers(values: ReadonlySet<LlmConsumer>): LlmConsumer[] {
  return (['agents', 'codegen'] as const).filter((consumer) => values.has(consumer))
}

export function ProjectLlmConnectionsCard() {
  const { active } = useWorkspace()
  const { identity } = useAuth()
  const canRead = hasWorkspaceRole(active, 'agents:read')
  const delegated =
    hasWorkspaceRole(active, 'agents:manage') &&
    hasWorkspaceRole(active, 'credentials:manage')
  const vault = active ? serviceConnection(active, 'llm-vault') : null

  const [editor, setEditor] = useState<LlmConnectionSummary | 'new' | null>(null)
  const [provider, setProvider] = useState<LlmProvider>('anthropic')
  const [label, setLabel] = useState('')
  const [consumers, setConsumers] = useState<Set<LlmConsumer>>(
    new Set(['agents', 'codegen']),
  )
  const [apiKey, setApiKey] = useState('')
  const apiKeyRef = useRef('')
  const [modelConnectionId, setModelConnectionId] = useState<string | null>(null)
  const [revokeTarget, setRevokeTarget] = useState<LlmConnectionSummary | null>(null)
  const [revokeReason, setRevokeReason] = useState('')
  const [pending, setPending] = useState<string | null>(null)
  const [dialogError, setDialogError] = useState<string | null>(null)

  const authorization = useQuery({
    queryKey: active ? queryKeys.projectAuthorization(active.id) : ['none', 'authorization'],
    enabled: active !== null && !delegated,
    queryFn: ({ signal }) => getProjectAuthorization(active!.projectId, signal),
  })
  const owner =
    authorization.data?.ownership.kind === 'human' &&
    authorization.data.ownership.owner_user_id === identity?.user_id
  const canManage = delegated || owner
  const canReadConnections = canRead || canManage

  const connections = useQuery({
    queryKey: active ? queryKeys.llmConnections(active.id) : ['none', 'llm-connections'],
    enabled: active !== null && vault !== null && canReadConnections,
    queryFn: ({ signal }) => listLlmConnections(vault!, active!.projectId, signal),
  })

  const detail = useQuery({
    queryKey:
      active && modelConnectionId
        ? queryKeys.llmModels(active.id, modelConnectionId)
        : ['none', 'llm-models'],
    enabled: active !== null && vault !== null && modelConnectionId !== null,
    queryFn: ({ signal }) =>
      getLlmConnection(vault!, modelConnectionId!, active!.projectId, signal),
  })

  const clearKey = () => {
    apiKeyRef.current = ''
    setApiKey('')
  }

  const closeEditor = () => {
    clearKey()
    setEditor(null)
    setDialogError(null)
  }

  const openEditor = (current: LlmConnectionSummary | 'new') => {
    setEditor(current)
    setProvider(current === 'new' ? 'anthropic' : current.provider)
    setLabel(current === 'new' ? '' : current.label)
    setConsumers(
      new Set(current === 'new' ? ['agents', 'codegen'] : current.consumers),
    )
    setDialogError(null)
    clearKey()
  }

  useEffect(() => {
    setEditor(null)
    setModelConnectionId(null)
    setRevokeTarget(null)
    setRevokeReason('')
    setPending(null)
    setDialogError(null)
    clearKey()
  }, [active?.id])

  useEffect(() => () => {
    apiKeyRef.current = ''
  }, [])

  if (!active) return null

  const refetch = () => void connections.refetch()

  const toggleConsumer = (consumer: LlmConsumer) => {
    setConsumers((current) => {
      const next = new Set(current)
      if (next.has(consumer)) next.delete(consumer)
      else next.add(consumer)
      return next
    })
  }

  const submitConnection = async (event: FormEvent) => {
    event.preventDefault()
    if (!vault || !editor || !canManage) return
    const selectedConsumers = canonicalConsumers(consumers)
    if (selectedConsumers.length === 0) {
      setDialogError('Select Agents, Codegen, or both.')
      return
    }
    const action = editor === 'new' ? 'create' : `replace:${editor.connection_id}`
    setPending(action)
    setDialogError(null)
    try {
      const saved =
        editor === 'new'
          ? await createLlmConnection(
              vault,
              active.projectId,
              provider,
              label.trim(),
              apiKeyRef.current,
              selectedConsumers,
            )
          : await replaceLlmConnection(
              vault,
              editor,
              label.trim(),
              apiKeyRef.current,
              selectedConsumers,
            )
      closeEditor()
      toast.success(`${saved.label} validated with ${saved.model_count} supported models`)
      refetch()
    } catch (error) {
      setDialogError(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) refetch()
    } finally {
      clearKey()
      setPending(null)
    }
  }

  const refresh = async (current: LlmConnectionSummary) => {
    if (!vault || !canManage) return
    setPending(`refresh:${current.connection_id}`)
    try {
      const saved = await refreshLlmConnection(vault, current)
      toast.success(`${saved.label} inventory refreshed (${saved.model_count} models)`)
      refetch()
      if (modelConnectionId === current.connection_id) void detail.refetch()
    } catch (error) {
      toast.error(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) refetch()
    } finally {
      setPending(null)
    }
  }

  const submitRevoke = async (event: FormEvent) => {
    event.preventDefault()
    if (!vault || !revokeTarget || !canManage) return
    const reason = revokeReason.trim()
    if (!reason) {
      setDialogError('Enter a revocation reason.')
      return
    }
    setPending(`revoke:${revokeTarget.connection_id}`)
    setDialogError(null)
    try {
      await revokeLlmConnection(vault, revokeTarget, reason)
      toast.success(`${revokeTarget.label} revoked and secret material destroyed`)
      setRevokeTarget(null)
      setRevokeReason('')
      if (modelConnectionId === revokeTarget.connection_id) setModelConnectionId(null)
      refetch()
    } catch (error) {
      setDialogError(errorMessage(error))
      if (error instanceof ApiError && error.status === 409) refetch()
    } finally {
      setPending(null)
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-4">
            <div>
              <CardTitle className="flex items-center gap-2">
                <KeyRound className="h-5 w-5" />
                Project LLM credential vault
              </CardTitle>
              <CardDescription className="mt-1">
                Store provider credentials once and grant each connection to Agents, Codegen,
                or both. Secret values are never shown again.
              </CardDescription>
            </div>
            {canManage ? (
              <Button size="sm" onClick={() => openEditor('new')}>
                <Plus /> Add connection
              </Button>
            ) : null}
          </div>
        </CardHeader>
        <CardContent>
          {!canReadConnections && authorization.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="animate-spin" /> Verifying vault access…
            </p>
          ) : !canReadConnections ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              Vault metadata requires <code>agents:read</code> or connection-management authority.
            </div>
          ) : connections.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="animate-spin" /> Loading vault connections…
            </p>
          ) : connections.error ? (
            <ErrorState error={connections.error} onRetry={() => void connections.refetch()} />
          ) : connections.data?.connections.length === 0 ? (
            <div className="rounded-md border border-dashed p-6 text-center text-sm text-muted-foreground">
              No provider credentials are stored for this project.
            </div>
          ) : (
            <div className="space-y-3">
              {connections.data?.connections.map((current) => {
                const activeConnection = current.state === 'active'
                return (
                  <div key={current.connection_id} className="rounded-md border p-4">
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div>
                        <div className="flex flex-wrap items-center gap-2">
                          <p className="font-medium">{current.label}</p>
                          <Badge variant="outline">{providerLabel(current.provider)}</Badge>
                          <Badge variant={activeConnection ? 'secondary' : 'outline'}>
                            {current.state}
                          </Badge>
                          {current.consumers.map((consumer) => (
                            <Badge key={consumer} variant="outline">{consumer}</Badge>
                          ))}
                        </div>
                        <p className="mt-1 text-xs text-muted-foreground">
                          Version {current.version} · {current.model_count} models · validated{' '}
                          <RelativeTime value={current.validated_at} />
                        </p>
                      </div>
                      <div className="flex flex-wrap gap-2">
                        {activeConnection ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setModelConnectionId(current.connection_id)}
                          >
                            <Eye /> Models
                          </Button>
                        ) : null}
                        {canManage && activeConnection ? (
                          <>
                            <Button
                              size="sm"
                              variant="outline"
                              disabled={pending !== null}
                              onClick={() => openEditor(current)}
                            >
                              Replace key
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={pending !== null}
                              onClick={() => void refresh(current)}
                            >
                              {pending === `refresh:${current.connection_id}` ? (
                                <Loader2 className="animate-spin" />
                              ) : (
                                <RefreshCw />
                              )}
                              Refresh
                            </Button>
                            <Button
                              size="sm"
                              variant="ghost"
                              disabled={pending !== null}
                              onClick={() => {
                                setRevokeTarget(current)
                                setDialogError(null)
                              }}
                            >
                              <Trash2 /> Revoke
                            </Button>
                          </>
                        ) : null}
                      </div>
                    </div>
                  </div>
                )
              })}
            </div>
          )}

          {!canManage && canReadConnections ? (
            <div className="mt-4 flex gap-2 rounded-md border border-dashed p-3 text-sm text-muted-foreground">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
              Connection changes require project ownership or both <code>agents:manage</code> and{' '}
              <code>credentials:manage</code>.
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Dialog open={editor !== null} onOpenChange={(open) => !open && closeEditor()}>
        <DialogContent>
          <form onSubmit={(event) => void submitConnection(event)}>
            <DialogHeader>
              <DialogTitle>{editor === 'new' ? 'Add vault connection' : 'Replace credential'}</DialogTitle>
              <DialogDescription>
                The vault validates the key and discovers supported models before committing any
                change.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-4 py-4">
              <div className="space-y-2">
                <Label htmlFor="vault-provider">Provider</Label>
                <select
                  id="vault-provider"
                  className="h-9 w-full rounded-md border bg-background px-3 text-sm"
                  value={provider}
                  disabled={editor !== 'new'}
                  onChange={(event) => setProvider(event.target.value as LlmProvider)}
                >
                  {PROVIDERS.map((item) => (
                    <option key={item.id} value={item.id}>{item.label}</option>
                  ))}
                </select>
              </div>
              <div className="space-y-2">
                <Label htmlFor="vault-label">Label</Label>
                <Input
                  id="vault-label"
                  value={label}
                  maxLength={80}
                  required
                  onChange={(event) => setLabel(event.target.value)}
                  placeholder="Production billing account"
                />
              </div>
              <div className="space-y-2">
                <Label htmlFor="vault-key">Provider API key</Label>
                <Input
                  id="vault-key"
                  type="password"
                  autoComplete="new-password"
                  value={apiKey}
                  required
                  onChange={(event) => {
                    apiKeyRef.current = event.target.value
                    setApiKey(event.target.value)
                  }}
                />
              </div>
              <fieldset className="space-y-2">
                <legend className="text-sm font-medium">Consumers</legend>
                {(['agents', 'codegen'] as const).map((consumer) => (
                  <label key={consumer} className="mr-5 inline-flex items-center gap-2 text-sm">
                    <input
                      type="checkbox"
                      checked={consumers.has(consumer)}
                      onChange={() => toggleConsumer(consumer)}
                    />
                    {consumer === 'agents' ? 'Agents' : 'Codegen'}
                  </label>
                ))}
              </fieldset>
              {dialogError ? <p className="text-sm text-destructive">{dialogError}</p> : null}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={closeEditor}>Cancel</Button>
              <Button type="submit" disabled={pending !== null}>Validate and save</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog open={modelConnectionId !== null} onOpenChange={(open) => !open && setModelConnectionId(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>{detail.data?.label ?? 'Supported models'}</DialogTitle>
            <DialogDescription>Provider inventory retained by the project vault.</DialogDescription>
          </DialogHeader>
          {detail.isPending ? (
            <p className="flex items-center gap-2 py-6 text-sm text-muted-foreground">
              <Loader2 className="animate-spin" /> Loading models…
            </p>
          ) : detail.error ? (
            <ErrorState error={detail.error} onRetry={() => void detail.refetch()} />
          ) : (
            <div className="max-h-80 space-y-2 overflow-y-auto">
              {detail.data?.models.map((model) => (
                <div key={model.model_id} className="rounded border px-3 py-2 font-mono text-sm">
                  {model.model_id}
                </div>
              ))}
            </div>
          )}
        </DialogContent>
      </Dialog>

      <Dialog open={revokeTarget !== null} onOpenChange={(open) => !open && setRevokeTarget(null)}>
        <DialogContent>
          <form onSubmit={(event) => void submitRevoke(event)}>
            <DialogHeader>
              <DialogTitle>Revoke {revokeTarget?.label}</DialogTitle>
              <DialogDescription>
                This removes all consumer grants and crypto-shreds the stored secret immediately.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-2 py-4">
              <Label htmlFor="vault-revoke-reason">Reason</Label>
              <Input
                id="vault-revoke-reason"
                value={revokeReason}
                required
                maxLength={2_000}
                onChange={(event) => setRevokeReason(event.target.value)}
              />
              {dialogError ? <p className="text-sm text-destructive">{dialogError}</p> : null}
            </div>
            <DialogFooter>
              <Button type="button" variant="outline" onClick={() => setRevokeTarget(null)}>Cancel</Button>
              <Button type="submit" variant="destructive" disabled={pending !== null}>Revoke</Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>
    </>
  )
}
