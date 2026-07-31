import { useQuery } from '@tanstack/react-query'
import {
  Bot,
  Eye,
  KeyRound,
  Loader2,
  RefreshCw,
  ShieldAlert,
  Trash2,
} from 'lucide-react'
import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { toast } from 'sonner'

import { ApiError } from '@/api/http'
import {
  getLlmModels,
  listLlmConnections,
  putLlmConnection,
  refreshLlmModels,
  revokeLlmConnection,
  type LlmConnectionSummary,
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
import {
  hasWorkspaceRole,
  serviceConnection,
  useWorkspace,
} from '@/core/workspace'

const PROVIDERS = [
  { id: 'openai', label: 'OpenAI' },
  { id: 'anthropic', label: 'Anthropic' },
  { id: 'google', label: 'Google' },
  { id: 'xai', label: 'xAI' },
] as const satisfies readonly { id: LlmProvider; label: string }[]

interface ConnectionTarget {
  provider: LlmProvider
  connection: LlmConnectionSummary | null
}

function providerLabel(provider: LlmProvider): string {
  return PROVIDERS.find((candidate) => candidate.id === provider)?.label ?? provider
}

function providerDetail(error: ApiError): string | null {
  if (typeof error.body !== 'object' || error.body === null) return null
  const detail = 'detail' in error.body ? error.body.detail : null
  if (typeof detail !== 'object' || detail === null || !('message' in detail)) return null
  return typeof detail.message === 'string' ? detail.message : null
}

function connectionErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    const detail = providerDetail(error)
    if (detail) return detail
    if (error.status === 403) {
      return 'Connection changes require project ownership or both agents:manage and credentials:manage.'
    }
    if (error.status === 409 || error.status === 404) return error.message
    if (error.status === 503 || error.status === 504) {
      return 'Provider validation is unavailable. The existing connection was not changed.'
    }
  }
  return 'The provider connection could not be changed. Try again shortly.'
}

function ConnectionState({ connection }: { connection: LlmConnectionSummary | null }) {
  if (!connection) return <Badge variant="outline">not connected</Badge>
  return (
    <Badge variant={connection.state === 'active' ? 'secondary' : 'outline'}>
      {connection.state}
    </Badge>
  )
}

export function ProjectLlmConnectionsCard() {
  const { active } = useWorkspace()
  const { identity } = useAuth()
  const canRead = hasWorkspaceRole(active, 'agents:read')
  const hasDelegatedAuthority =
    hasWorkspaceRole(active, 'agents:manage') &&
    hasWorkspaceRole(active, 'credentials:manage')
  const connection = active ? serviceConnection(active, 'agents') : null
  const [connectionTarget, setConnectionTarget] = useState<ConnectionTarget | null>(null)
  const [apiKey, setApiKey] = useState('')
  const apiKeyRef = useRef('')
  const [revokeTarget, setRevokeTarget] = useState<LlmConnectionSummary | null>(null)
  const [revokeReason, setRevokeReason] = useState('')
  const [modelsProvider, setModelsProvider] = useState<LlmProvider | null>(null)
  const [pendingAction, setPendingAction] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)

  const authorization = useQuery({
    queryKey: active ? queryKeys.projectAuthorization(active.id) : ['none', 'authorization'],
    enabled: active !== null && !hasDelegatedAuthority,
    queryFn: ({ signal }) => getProjectAuthorization(active!.projectId, signal),
  })
  const isOwner =
    authorization.data?.ownership.kind === 'human' &&
    authorization.data.ownership.owner_user_id === identity?.user_id
  const canManage = hasDelegatedAuthority || isOwner
  const canReadConnections = canRead || canManage

  const connections = useQuery({
    queryKey: active ? queryKeys.llmConnections(active.id) : ['none', 'llm-connections'],
    enabled: active !== null && canReadConnections,
    queryFn: ({ signal }) =>
      listLlmConnections(connection!, active!.projectId, signal),
  })

  const models = useQuery({
    queryKey:
      active && modelsProvider
        ? queryKeys.llmModels(active.id, modelsProvider)
        : ['none', 'llm-models'],
    enabled:
      active !== null &&
      connection !== null &&
      modelsProvider !== null &&
      canReadConnections,
    queryFn: ({ signal }) =>
      getLlmModels(connection!, modelsProvider!, active!.projectId, signal),
  })

  const connectionByProvider = useMemo(
    () =>
      new Map(
        (connections.data?.connections ?? []).map((item) => [item.provider, item] as const),
      ),
    [connections.data?.connections],
  )

  const clearApiKey = () => {
    apiKeyRef.current = ''
    setApiKey('')
  }

  const closeConnectionDialog = () => {
    clearApiKey()
    setConnectionTarget(null)
    setActionError(null)
  }

  const closeRevokeDialog = () => {
    setRevokeTarget(null)
    setRevokeReason('')
    setActionError(null)
  }

  useEffect(() => {
    clearApiKey()
    setConnectionTarget(null)
    setRevokeTarget(null)
    setRevokeReason('')
    setModelsProvider(null)
    setPendingAction(null)
    setActionError(null)
  }, [active?.id])

  useEffect(
    () => () => {
      apiKeyRef.current = ''
    },
    [],
  )

  if (!active) return null

  const refetchConnections = () => {
    void connections.refetch()
  }

  const submitConnection = async (event: FormEvent) => {
    event.preventDefault()
    if (!connectionTarget || !connection || !canManage) return
    const { provider, connection: current } = connectionTarget
    const submittedKey = apiKeyRef.current
    setPendingAction(`put:${provider}`)
    setActionError(null)
    try {
      const saved = await putLlmConnection(
        connection,
        provider,
        active.projectId,
        submittedKey,
        current?.version ?? 0,
      )
      closeConnectionDialog()
      toast.success(
        `${providerLabel(provider)} connected with ${saved.models.length} supported ${
          saved.models.length === 1 ? 'model' : 'models'
        }`,
      )
      refetchConnections()
      if (modelsProvider === provider) void models.refetch()
    } catch (error) {
      setActionError(connectionErrorMessage(error))
      if (error instanceof ApiError && error.status === 409) {
        const refreshed = await connections.refetch()
        const latest =
          refreshed.data?.connections.find((item) => item.provider === provider) ?? current
        setConnectionTarget((target) =>
          target?.provider === provider ? { ...target, connection: latest } : target,
        )
      }
    } finally {
      clearApiKey()
      setPendingAction(null)
    }
  }

  const refreshModels = async (current: LlmConnectionSummary) => {
    if (!connection || !canManage) return
    setPendingAction(`refresh:${current.provider}`)
    setActionError(null)
    try {
      const refreshed = await refreshLlmModels(
        connection,
        current.provider,
        active.projectId,
        current.version,
      )
      toast.success(
        `${providerLabel(current.provider)} model inventory refreshed (${refreshed.model_count})`,
      )
      refetchConnections()
      if (modelsProvider === current.provider) void models.refetch()
    } catch (error) {
      toast.error(connectionErrorMessage(error))
      if (error instanceof ApiError && error.status === 409) refetchConnections()
    } finally {
      setPendingAction(null)
    }
  }

  const submitRevoke = async (event: FormEvent) => {
    event.preventDefault()
    if (!connection || !revokeTarget || !canManage) return
    const reason = revokeReason.trim()
    if (!reason) {
      setActionError('Enter a reason for revoking this provider connection.')
      return
    }
    setPendingAction(`revoke:${revokeTarget.provider}`)
    setActionError(null)
    try {
      await revokeLlmConnection(
        connection,
        revokeTarget.provider,
        active.projectId,
        revokeTarget.version,
        reason,
      )
      const label = providerLabel(revokeTarget.provider)
      closeRevokeDialog()
      toast.success(`${label} connection revoked`)
      refetchConnections()
      if (modelsProvider === revokeTarget.provider) setModelsProvider(null)
    } catch (error) {
      setActionError(connectionErrorMessage(error))
      if (error instanceof ApiError && error.status === 409) {
        const refreshed = await connections.refetch()
        const latest = refreshed.data?.connections.find(
          (item) => item.provider === revokeTarget.provider,
        )
        if (latest) setRevokeTarget(latest)
      }
    } finally {
      setPendingAction(null)
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <KeyRound className="h-5 w-5" />
            LLM provider connections
          </CardTitle>
          <CardDescription>
            Validate and store one encrypted API key per provider for project{' '}
            <span className="font-mono">{active.projectId}</span>. Keys are never shown again.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {!canReadConnections && authorization.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
              <Loader2 className="animate-spin" /> Verifying LLM connection access…
            </p>
          ) : !canReadConnections ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              Provider connections require <code>agents:read</code>, project ownership, or both{' '}
              <code>agents:manage</code> and <code>credentials:manage</code>. No connection
              metadata or management controls are available to this membership.
            </div>
          ) : connections.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
              <Loader2 className="animate-spin" /> Loading LLM connections…
            </p>
          ) : connections.error ? (
            <ErrorState error={connections.error} onRetry={() => void connections.refetch()} />
          ) : (
            <div className="space-y-4">
              <div className="grid gap-3 md:grid-cols-2">
                {PROVIDERS.map((provider) => {
                  const current = connectionByProvider.get(provider.id) ?? null
                  const activeConnection = current?.state === 'active'
                  return (
                    <div key={provider.id} className="rounded-md border p-4">
                      <div className="flex items-start justify-between gap-3">
                        <div>
                          <p className="font-medium">{provider.label}</p>
                          {current ? (
                            <p className="mt-1 text-xs text-muted-foreground">
                              Version {current.version} ·{' '}
                              {activeConnection ? (
                                <>
                                  {current.model_count}{' '}
                                  {current.model_count === 1 ? 'model' : 'models'} · validated{' '}
                                  <RelativeTime value={current.validated_at} />
                                </>
                              ) : (
                                <>
                                  revoked <RelativeTime value={current.revoked_at!} />
                                </>
                              )}
                            </p>
                          ) : (
                            <p className="mt-1 text-xs text-muted-foreground">
                              No project credential stored
                            </p>
                          )}
                        </div>
                        <ConnectionState connection={current} />
                      </div>

                      <div className="mt-4 flex flex-wrap gap-2">
                        {activeConnection ? (
                          <Button
                            size="sm"
                            variant="outline"
                            onClick={() => setModelsProvider(provider.id)}
                          >
                            <Eye />
                            View models
                          </Button>
                        ) : null}
                        {canManage ? (
                          <>
                            <Button
                              size="sm"
                              variant={current ? 'outline' : 'default'}
                              disabled={pendingAction !== null}
                              onClick={() =>
                                setConnectionTarget({
                                  provider: provider.id,
                                  connection: current,
                                })
                              }
                            >
                              {current
                                ? activeConnection
                                  ? 'Replace key'
                                  : 'Reconnect'
                                : 'Connect'}
                            </Button>
                            {activeConnection ? (
                              <>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={pendingAction !== null}
                                  onClick={() => void refreshModels(current)}
                                >
                                  {pendingAction === `refresh:${provider.id}` ? (
                                    <Loader2 className="animate-spin" />
                                  ) : (
                                    <RefreshCw />
                                  )}
                                  Refresh
                                </Button>
                                <Button
                                  size="sm"
                                  variant="ghost"
                                  disabled={pendingAction !== null}
                                  onClick={() => setRevokeTarget(current)}
                                >
                                  <Trash2 />
                                  Revoke
                                </Button>
                              </>
                            ) : null}
                          </>
                        ) : null}
                      </div>
                    </div>
                  )
                })}
              </div>

              {!canManage && authorization.isPending ? (
                <div className="flex gap-2 rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                  <Loader2 className="mt-0.5 h-4 w-4 shrink-0 animate-spin" />
                  Verifying LLM connection management authority…
                </div>
              ) : !canManage ? (
                <div className="flex gap-2 rounded-md border border-dashed p-3 text-sm text-muted-foreground">
                  <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
                  <p>
                    Connection changes require project ownership or both{' '}
                    <code>agents:manage</code> and <code>credentials:manage</code>.
                    {authorization.error
                      ? ' Project ownership could not be verified, so controls are hidden.'
                      : ''}
                  </p>
                </div>
              ) : (
                <p className="text-xs text-muted-foreground">
                  Connecting a provider discovers APDL-supported models. It does not activate
                  Agents, assign models, or authorize Codegen.
                </p>
              )}
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog
        open={connectionTarget !== null}
        onOpenChange={(open) => {
          if (!open) closeConnectionDialog()
        }}
      >
        <DialogContent>
          <form onSubmit={(event) => void submitConnection(event)} className="space-y-4">
            <DialogHeader>
              <DialogTitle>
                {connectionTarget?.connection?.state === 'active' ? 'Replace' : 'Connect'}{' '}
                {connectionTarget ? providerLabel(connectionTarget.provider) : ''} key
              </DialogTitle>
              <DialogDescription>
                Agents makes one bounded provider request to validate this key and discover
                supported models, then stores it encrypted for this project.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="llm-provider-api-key">Provider API key</Label>
              <Input
                id="llm-provider-api-key"
                type="password"
                autoComplete="off"
                value={apiKey}
                disabled={pendingAction !== null}
                onChange={(event) => {
                  apiKeyRef.current = event.target.value
                  setApiKey(event.target.value)
                }}
              />
              <p className="text-xs text-muted-foreground">
                The raw key is cleared from this form after every submission and is never returned
                by APDL.
              </p>
            </div>
            {actionError ? <p className="text-sm text-destructive">{actionError}</p> : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={pendingAction !== null}
                onClick={closeConnectionDialog}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={pendingAction !== null || apiKey.length === 0}>
                {pendingAction?.startsWith('put:') ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <Bot />
                )}
                Validate and save
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open) closeRevokeDialog()
        }}
      >
        <DialogContent>
          <form onSubmit={(event) => void submitRevoke(event)} className="space-y-4">
            <DialogHeader>
              <DialogTitle>
                Revoke {revokeTarget ? providerLabel(revokeTarget.provider) : ''} connection?
              </DialogTitle>
              <DialogDescription>
                Revocation destroys the encrypted credential and model inventory. It is blocked
                while an active model assignment still uses this provider.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="llm-revoke-reason">Reason</Label>
              <Input
                id="llm-revoke-reason"
                value={revokeReason}
                maxLength={2_000}
                disabled={pendingAction !== null}
                onChange={(event) => setRevokeReason(event.target.value)}
                placeholder="Credential rotated outside APDL"
              />
            </div>
            {actionError ? <p className="text-sm text-destructive">{actionError}</p> : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={pendingAction !== null}
                onClick={closeRevokeDialog}
              >
                Cancel
              </Button>
              <Button
                type="submit"
                variant="destructive"
                disabled={pendingAction !== null || revokeReason.trim().length === 0}
              >
                {pendingAction?.startsWith('revoke:') ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <Trash2 />
                )}
                Revoke connection
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={modelsProvider !== null}
        onOpenChange={(open) => {
          if (!open) setModelsProvider(null)
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>
              {modelsProvider ? providerLabel(modelsProvider) : ''} supported models
            </DialogTitle>
            <DialogDescription>
              Models accessible to this project key and allowlisted by the APDL provider catalog.
            </DialogDescription>
          </DialogHeader>
          {models.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
              <Loader2 className="animate-spin" /> Loading model inventory…
            </p>
          ) : models.error ? (
            <ErrorState error={models.error} onRetry={() => void models.refetch()} />
          ) : (
            <div className="max-h-[60vh] space-y-2 overflow-y-auto">
              {models.data?.models.map((model) => (
                <div key={model.model_id} className="rounded-md border p-3">
                  <div className="flex flex-wrap items-start justify-between gap-2">
                    <div>
                      <p className="font-medium">{model.display_name}</p>
                      <p className="mt-1 font-mono text-xs text-muted-foreground">
                        {model.model_id}
                      </p>
                    </div>
                    <div className="flex gap-1">
                      {model.supported_tiers.map((tier) => (
                        <Badge key={tier} variant="secondary">
                          {tier}
                        </Badge>
                      ))}
                    </div>
                  </div>
                  <p className="mt-2 text-xs text-muted-foreground">
                    Data residency: {model.data_residency} · classifications:{' '}
                    {model.allowed_data_classifications.join(', ')} · pricing requires operator
                    review
                  </p>
                </div>
              ))}
            </div>
          )}
          <DialogFooter>
            <Button type="button" variant="outline" onClick={() => setModelsProvider(null)}>
              Close
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
