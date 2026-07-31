import {
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  ShieldAlert,
  Trash2,
} from 'lucide-react'
import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type FormEvent,
} from 'react'
import { toast } from 'sonner'

import {
  providerDiscoveryErrorCode,
  putProjectLlmConnection,
  refreshProjectLlmModels,
  revokeProjectLlmConnection,
} from '@/api/agentsSetup'
import { ApiError } from '@/api/http'
import type {
  LlmConnectionSummary,
  LlmProvider,
} from '@/api/types/agents-setup'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
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
import { Select } from '@/components/ui/select'
import { Textarea } from '@/components/ui/textarea'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import {
  useAgentsConnections,
  useInvalidateAgentsConnections,
} from '@/features/agents/setup/hooks'

const PROVIDERS: readonly LlmProvider[] = [
  'openai',
  'anthropic',
  'google',
  'xai',
]

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  xai: 'xAI',
}

const PROVIDER_KEY_HINTS: Record<LlmProvider, string> = {
  openai: 'OpenAI API key',
  anthropic: 'Anthropic API key',
  google: 'Google AI API key',
  xai: 'xAI API key',
}

const PROVIDER_ERROR_MESSAGES = {
  invalid_key: 'The provider rejected this API key.',
  permission_denied:
    'The API key is valid but cannot list the supported models.',
  rate_limited:
    'The provider rate-limited model discovery. Wait briefly and try again.',
  provider_timeout:
    'The provider did not answer in time. Your existing connection is unchanged.',
  provider_unavailable:
    'The provider is temporarily unavailable. Your existing connection is unchanged.',
  malformed_response:
    'The provider returned an invalid model catalog response.',
  no_supported_models:
    'This API key cannot access any APDL-supported models.',
} as const

function connectionError(error: unknown): string {
  const discoveryCode = providerDiscoveryErrorCode(error)
  if (discoveryCode) return PROVIDER_ERROR_MESSAGES[discoveryCode]
  if (error instanceof ApiError && error.status === 409) {
    return error.message
  }
  if (error instanceof ApiError && error.status === 403) {
    return 'Your live project authority no longer permits connection management.'
  }
  if (error instanceof ApiError) return error.message
  return 'The provider connection operation failed. Try again shortly.'
}

function providerVersion(
  connections: readonly LlmConnectionSummary[],
  provider: LlmProvider,
): number {
  return connections.find((connection) => connection.provider === provider)
    ?.version ?? 0
}

export function LlmConnectionsManager({
  canManage,
  onChanged,
  assignedProviders = [],
}: {
  canManage: boolean
  onChanged?: () => void
  assignedProviders?: readonly LlmProvider[]
}) {
  const { active, projectId } = useWorkspace()
  const connectionsQuery = useAgentsConnections()
  const invalidateConnections = useInvalidateAgentsConnections()
  const [formOpen, setFormOpen] = useState(false)
  const [provider, setProvider] = useState<LlmProvider>('openai')
  const [pending, setPending] = useState(false)
  const [pendingProvider, setPendingProvider] =
    useState<LlmProvider | null>(null)
  const [formError, setFormError] = useState<string | null>(null)
  const [revokeTarget, setRevokeTarget] =
    useState<LlmConnectionSummary | null>(null)
  const [revokeReason, setRevokeReason] = useState('')
  const keyInputRef = useRef<HTMLInputElement | null>(null)
  const connectionRequestRef = useRef<AbortController | null>(null)
  const setKeyInputRef = useCallback((node: HTMLInputElement | null) => {
    if (node === null && keyInputRef.current) {
      keyInputRef.current.value = ''
    }
    keyInputRef.current = node
  }, [])

  const clearKeyInput = () => {
    if (keyInputRef.current) keyInputRef.current.value = ''
  }

  useEffect(() => {
    connectionRequestRef.current?.abort()
    connectionRequestRef.current = null
    clearKeyInput()
    setFormOpen(false)
    setPending(false)
    setPendingProvider(null)
    setFormError(null)
    setRevokeTarget(null)
    setRevokeReason('')
  }, [active?.id])

  useEffect(
    () => () => {
      connectionRequestRef.current?.abort()
      connectionRequestRef.current = null
      clearKeyInput()
    },
    [],
  )

  const connections = connectionsQuery.data?.connections ?? []
  const selectedExisting = connections.find(
    (connection) => connection.provider === provider,
  )

  const refreshState = async () => {
    await invalidateConnections()
    onChanged?.()
  }

  const closeForm = () => {
    clearKeyInput()
    setFormOpen(false)
    setFormError(null)
  }

  const submitConnection = async (event: FormEvent) => {
    event.preventDefault()
    if (!active || !projectId || !canManage || !keyInputRef.current) return
    if (!keyInputRef.current.value) {
      setFormError('Enter the provider API key.')
      return
    }
    const controller = new AbortController()
    connectionRequestRef.current?.abort()
    connectionRequestRef.current = controller
    setPending(true)
    setPendingProvider(provider)
    setFormError(null)
    try {
      const response = await putProjectLlmConnection(
        serviceConnection(active, 'agents'),
        projectId,
        provider,
        keyInputRef.current.value,
        providerVersion(connections, provider),
        { signal: controller.signal },
      )
      // The request boundary is the last place the plaintext key is needed.
      // Clear the DOM before any cache or identity synchronization awaits.
      clearKeyInput()
      if (controller.signal.aborted) return
      await refreshState()
      if (controller.signal.aborted) return
      setFormOpen(false)
      toast.success(
        `${PROVIDER_LABELS[provider]} connected with ${response.model_count} supported models`,
      )
    } catch (error) {
      clearKeyInput()
      if (controller.signal.aborted) return
      setFormError(connectionError(error))
      if (error instanceof ApiError && error.status === 409) {
        await refreshState()
      }
    } finally {
      clearKeyInput()
      if (connectionRequestRef.current === controller) {
        connectionRequestRef.current = null
        setPending(false)
        setPendingProvider(null)
      }
    }
  }

  const refreshModels = async (connection: LlmConnectionSummary) => {
    if (!active || !projectId || !canManage) return
    setPending(true)
    setPendingProvider(connection.provider)
    setFormError(null)
    try {
      const response = await refreshProjectLlmModels(
        serviceConnection(active, 'agents'),
        projectId,
        connection.provider,
        connection.version,
      )
      await refreshState()
      toast.success(
        `${PROVIDER_LABELS[connection.provider]} inventory refreshed (${response.model_count} models)`,
      )
    } catch (error) {
      toast.error(connectionError(error))
      if (error instanceof ApiError && error.status === 409) {
        await refreshState()
      }
    } finally {
      setPending(false)
      setPendingProvider(null)
    }
  }

  const confirmRevoke = async () => {
    if (
      !active ||
      !projectId ||
      !canManage ||
      !revokeTarget ||
      !revokeReason.trim()
    ) {
      return
    }
    setPending(true)
    setPendingProvider(revokeTarget.provider)
    try {
      await revokeProjectLlmConnection(
        serviceConnection(active, 'agents'),
        projectId,
        revokeTarget.provider,
        revokeTarget.version,
        revokeReason.trim(),
      )
      await refreshState()
      toast.success(`${PROVIDER_LABELS[revokeTarget.provider]} connection revoked`)
      setRevokeTarget(null)
      setRevokeReason('')
    } catch (error) {
      toast.error(connectionError(error))
      if (error instanceof ApiError && error.status === 409) {
        await refreshState()
      }
    } finally {
      setPending(false)
      setPendingProvider(null)
    }
  }

  return (
    <div className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div>
          <h4 className="text-sm font-semibold">Provider connections</h4>
          <p className="mt-1 text-xs text-muted-foreground">
            Keys are encrypted by the Agents service and are never shown again
            or retained in browser query state.
          </p>
        </div>
        {canManage ? (
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              setFormError(null)
              setFormOpen(true)
            }}
          >
            <Plus />
            Add provider
          </Button>
        ) : null}
      </div>

      {connectionsQuery.isPending ? (
        <div
          className="flex items-center gap-2 rounded-md border p-4 text-sm text-muted-foreground"
          role="status"
        >
          <Loader2 className="h-4 w-4 animate-spin" />
          Loading provider connections…
        </div>
      ) : connectionsQuery.isError ? (
        <ErrorState
          error={connectionsQuery.error}
          onRetry={() => void connectionsQuery.refetch()}
        />
      ) : connections.length === 0 ? (
        <div className="rounded-md border border-dashed p-5 text-center">
          <KeyRound className="mx-auto h-6 w-6 text-muted-foreground" />
          <p className="mt-2 text-sm font-medium">No LLM provider connected</p>
          <p className="mt-1 text-xs text-muted-foreground">
            Connect at least one provider before choosing fast and reasoning
            models.
          </p>
        </div>
      ) : (
        <div className="divide-y rounded-md border">
          {connections.map((connection) => (
            <div
              key={connection.provider}
              className="flex flex-wrap items-center justify-between gap-3 p-3"
            >
              <div className="min-w-0">
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">
                    {PROVIDER_LABELS[connection.provider]}
                  </span>
                  <Badge
                    variant={
                      connection.state === 'active'
                        ? 'default'
                        : 'secondary'
                    }
                  >
                    {connection.state}
                  </Badge>
                  <Badge variant="outline">
                    {connection.model_count}{' '}
                    {connection.model_count === 1 ? 'model' : 'models'}
                  </Badge>
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  Inventory v{connection.inventory_version} · validated{' '}
                  <RelativeTime value={connection.validated_at} />
                </p>
              </div>
              {canManage ? (
                <div className="flex flex-wrap gap-1">
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={
                      pending || connection.state !== 'active'
                    }
                    onClick={() => void refreshModels(connection)}
                  >
                    {pendingProvider === connection.provider && pending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <RefreshCw />
                    )}
                    Refresh
                  </Button>
                  <Button
                    type="button"
                    size="sm"
                    variant="ghost"
                    disabled={pending}
                    onClick={() => {
                      clearKeyInput()
                      setProvider(connection.provider)
                      setFormError(null)
                      setFormOpen(true)
                    }}
                  >
                    <KeyRound />
                    {connection.state === 'active' ? 'Replace key' : 'Reconnect'}
                  </Button>
                  {connection.state === 'active' ? (
                    <Button
                      type="button"
                      size="sm"
                      variant="ghost"
                      className="text-destructive hover:text-destructive"
                      disabled={
                        pending ||
                        assignedProviders.includes(connection.provider)
                      }
                      title={
                        assignedProviders.includes(connection.provider)
                          ? 'Reassign every tier using this provider before revoking it'
                          : undefined
                      }
                      onClick={() => {
                        setRevokeReason('')
                        setRevokeTarget(connection)
                      }}
                    >
                      <Trash2 />
                      {assignedProviders.includes(connection.provider)
                        ? 'Reassign first'
                        : 'Revoke'}
                    </Button>
                  ) : null}
                </div>
              ) : null}
            </div>
          ))}
        </div>
      )}

      {!canManage ? (
        <p className="flex gap-2 rounded-md border bg-muted/30 p-3 text-xs text-muted-foreground">
          <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0" />
          Provider metadata is visible, but changes require project ownership
          or both agents:manage and credentials:manage.
        </p>
      ) : null}

      <Dialog
        open={formOpen}
        onOpenChange={(open) => {
          if (open) setFormOpen(true)
          else closeForm()
        }}
      >
        <DialogContent>
          <form
            onSubmit={(event) => void submitConnection(event)}
            className="space-y-4"
            noValidate
          >
            <DialogHeader>
              <DialogTitle>
                {selectedExisting?.state === 'active'
                  ? `Replace ${PROVIDER_LABELS[provider]} key`
                  : 'Connect an LLM provider'}
              </DialogTitle>
              <DialogDescription>
                APDL validates the key and stores the supported model inventory.
                The key leaves this form only in the connection request.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="llm-provider">Provider</Label>
              <Select
                id="llm-provider"
                value={provider}
                disabled={pending}
                onChange={(event) => {
                  clearKeyInput()
                  setProvider(event.target.value as LlmProvider)
                  setFormError(null)
                }}
              >
                {PROVIDERS.map((option) => (
                  <option key={option} value={option}>
                    {PROVIDER_LABELS[option]}
                  </option>
                ))}
              </Select>
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="llm-provider-key">
                {PROVIDER_KEY_HINTS[provider]}
              </Label>
              <Input
                id="llm-provider-key"
                ref={setKeyInputRef}
                type="password"
                autoComplete="new-password"
                spellCheck={false}
                disabled={pending}
                aria-invalid={formError ? true : undefined}
                aria-describedby={
                  formError
                    ? 'llm-provider-key-help llm-provider-key-error'
                    : 'llm-provider-key-help'
                }
              />
              <p
                id="llm-provider-key-help"
                className="text-xs text-muted-foreground"
              >
                Never displayed again. Replacing a key invalidates the prior
                connection version after validation succeeds.
              </p>
            </div>
            {formError ? (
              <p
                id="llm-provider-key-error"
                className="text-sm text-destructive"
                role="alert"
              >
                {formError}
              </p>
            ) : null}
            <DialogFooter>
              <Button
                type="button"
                variant="outline"
                disabled={pending}
                onClick={closeForm}
              >
                Cancel
              </Button>
              <Button type="submit" disabled={pending}>
                {pending ? <Loader2 className="animate-spin" /> : null}
                Validate and connect
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={revokeTarget !== null}
        onOpenChange={(open) => {
          if (!open && !pending) {
            setRevokeTarget(null)
            setRevokeReason('')
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              Revoke{' '}
              {revokeTarget
                ? PROVIDER_LABELS[revokeTarget.provider]
                : 'provider'}{' '}
              connection?
            </DialogTitle>
            <DialogDescription>
              The stored provider credential becomes unusable immediately.
              Active Agents setup may become blocked.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="llm-revoke-reason">Reason</Label>
            <Textarea
              id="llm-revoke-reason"
              value={revokeReason}
              maxLength={2_000}
              disabled={pending}
              onChange={(event) => setRevokeReason(event.target.value)}
              placeholder="Why is this connection being revoked?"
            />
          </div>
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={pending}
              onClick={() => {
                setRevokeTarget(null)
                setRevokeReason('')
              }}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant="destructive"
              disabled={pending || !revokeReason.trim()}
              onClick={() => void confirmRevoke()}
            >
              {pending ? <Loader2 className="animate-spin" /> : null}
              Revoke connection
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
