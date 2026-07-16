import { useQuery } from '@tanstack/react-query'
import {
  History,
  KeyRound,
  Loader2,
  Plus,
  RefreshCw,
  ShieldAlert,
  Trash2,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { toast } from 'sonner'

import {
  BROWSER_CREDENTIAL_ROLES,
  CREDENTIAL_ROLE_ORDER,
  createProjectCredential,
  credentialCreateRequestSchema,
  getProjectCredentialAudit,
  listProjectCredentials,
  revokeProjectCredential,
  rotateProjectCredential,
  type CredentialAuditEntry,
  type CredentialKind,
  type CredentialMetadata,
  type CredentialReveal,
  type CredentialRole,
} from '@/api/credentials'
import { ApiError } from '@/api/http'
import { CopyButton } from '@/components/shared/CopyButton'
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
import { Select } from '@/components/ui/select'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { queryKeys } from '@/core/queryClient'
import { hasWorkspaceRole, useWorkspace } from '@/core/workspace'

const ROLE_DESCRIPTIONS: Record<CredentialRole, string> = {
  'events:write': 'Publish product events',
  'config:read': 'Read flags and open configuration streams',
  'config:evaluate': 'Evaluate flags on a trusted server',
  'query:read': 'Run analytics queries',
}

type PendingAction =
  | { type: 'rotate'; credential: CredentialMetadata }
  | { type: 'revoke'; credential: CredentialMetadata }

interface AuditState {
  credential: CredentialMetadata
  entries: CredentialAuditEntry[]
  loading: boolean
  error: string | null
}

interface RevealState {
  operation: 'created' | 'rotated'
  credential: CredentialReveal
}

function actionErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return 'Your current project membership does not permit credential management.'
  }
  return 'The credential operation failed. Try again shortly.'
}

function CredentialRoles({ roles }: { roles: readonly CredentialRole[] }) {
  return (
    <div className="flex flex-wrap gap-1">
      {roles.map((role) => (
        <Badge key={role} variant="secondary" className="font-mono text-[11px]">
          {role}
        </Badge>
      ))}
    </div>
  )
}

function SecretRevealDialog({
  reveal,
  onClose,
}: {
  reveal: RevealState | null
  onClose: () => void
}) {
  return (
    <Dialog open={reveal !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Copy this API key now</DialogTitle>
          <DialogDescription>
            {reveal?.operation === 'rotated'
              ? 'The successor credential is ready. The previous credential remains active until you revoke it.'
              : 'The credential is ready.'}{' '}
            APDL will not display this secret again.
          </DialogDescription>
        </DialogHeader>
        {reveal ? (
          <div className="space-y-4">
            <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
              Store this key in your application secret manager. Do not put it in source control,
              browser storage, logs, or screenshots.
            </div>
            <div className="space-y-1.5">
              <Label htmlFor="revealed-api-key">API key</Label>
              <div className="flex gap-2">
                <Input
                  id="revealed-api-key"
                  className="font-mono"
                  value={reveal.credential.api_key}
                  readOnly
                  autoComplete="off"
                  spellCheck={false}
                />
                <CopyButton
                  value={reveal.credential.api_key}
                  label="Copy API key"
                  className="h-9 w-9 rounded-md border"
                />
              </div>
            </div>
            <div className="space-y-2 text-sm">
              <p>
                Credential{' '}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">
                  {reveal.credential.credential_id}
                </code>
              </p>
              <CredentialRoles roles={reveal.credential.roles} />
            </div>
          </div>
        ) : null}
        <DialogFooter>
          <Button type="button" onClick={onClose}>
            I have saved the key
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

function AuditDialog({
  state,
  onClose,
}: {
  state: AuditState | null
  onClose: () => void
}) {
  return (
    <Dialog open={state !== null} onOpenChange={(open) => !open && onClose()}>
      <DialogContent className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>Credential audit</DialogTitle>
          <DialogDescription>
            Immutable lifecycle history for{' '}
            <code className="rounded bg-muted px-1 py-0.5 text-xs">
              {state?.credential.credential_id}
            </code>
            .
          </DialogDescription>
        </DialogHeader>
        {state?.loading ? (
          <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
            <Loader2 className="animate-spin" />
            Loading audit history…
          </div>
        ) : state?.error ? (
          <p className="py-6 text-sm text-destructive">{state.error}</p>
        ) : state?.entries.length === 0 ? (
          <p className="py-6 text-sm text-muted-foreground">No audit entries were returned.</p>
        ) : (
          <div className="max-h-96 space-y-3 overflow-y-auto">
            {state?.entries.map((entry) => (
              <div key={entry.audit_id} className="rounded-md border p-3 text-sm">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <Badge variant="outline">{entry.action}</Badge>
                  <RelativeTime value={entry.created_at} className="text-muted-foreground" />
                </div>
                <p className="mt-2 text-muted-foreground">
                  {entry.actor_email}
                  {entry.successor_credential_id ? (
                    <>
                      {' · successor '}
                      <code className="font-mono text-xs">{entry.successor_credential_id}</code>
                    </>
                  ) : null}
                </p>
                <div className="mt-2">
                  <CredentialRoles roles={entry.roles} />
                </div>
              </div>
            ))}
          </div>
        )}
        <DialogFooter>
          <Button type="button" variant="outline" onClick={onClose}>
            Close
          </Button>
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ProjectCredentialsCard() {
  const { active } = useWorkspace()
  const canManage = hasWorkspaceRole(active, 'credentials:manage')
  const browserScopeAvailable = BROWSER_CREDENTIAL_ROLES.every((role) =>
    hasWorkspaceRole(active, role),
  )
  const [createOpen, setCreateOpen] = useState(false)
  const [kind, setKind] = useState<CredentialKind>('browser')
  const [confidentialRoles, setConfidentialRoles] = useState<CredentialRole[]>([])
  const [creating, setCreating] = useState(false)
  const [createError, setCreateError] = useState<string | null>(null)
  const [pendingAction, setPendingAction] = useState<PendingAction | null>(null)
  const [actionPending, setActionPending] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const [audit, setAudit] = useState<AuditState | null>(null)
  const [reveal, setReveal] = useState<RevealState | null>(null)
  const revealRef = useRef<RevealState | null>(null)
  const selectedConfidentialRoles = CREDENTIAL_ROLE_ORDER.filter(
    (role) => confidentialRoles.includes(role) && hasWorkspaceRole(active, role),
  )

  const query = useQuery({
    queryKey: active ? queryKeys.credentials(active.id) : ['none', 'credentials'],
    enabled: active !== null && canManage,
    queryFn: () => listProjectCredentials(active!.projectId),
  })

  const clearReveal = useCallback(() => {
    revealRef.current = null
    setReveal(null)
  }, [])

  const showReveal = (next: RevealState) => {
    revealRef.current = next
    setReveal(next)
  }

  useEffect(() => {
    clearReveal()
    setCreateOpen(false)
    setPendingAction(null)
    setAudit(null)
  }, [active?.id, clearReveal])

  useEffect(
    () => () => {
      revealRef.current = null
    },
    [],
  )

  const closeCreate = () => {
    setCreateOpen(false)
    setKind('browser')
    setConfidentialRoles([])
    setCreateError(null)
  }

  const submitCreate = async (event: FormEvent) => {
    event.preventDefault()
    if (!active || !canManage) return
    if (kind === 'browser' && !browserScopeAvailable) {
      setCreateError('Your membership does not grant the fixed browser credential scope.')
      return
    }
    const roles =
      kind === 'browser'
        ? [...BROWSER_CREDENTIAL_ROLES]
        : selectedConfidentialRoles
    const parsed = credentialCreateRequestSchema.safeParse({ credential_kind: kind, roles })
    if (!parsed.success) {
      setCreateError(
        kind === 'confidential'
          ? 'Select at least one server role.'
          : 'The browser credential role contract is invalid.',
      )
      return
    }

    setCreating(true)
    setCreateError(null)
    try {
      const created = await createProjectCredential(active.projectId, parsed.data)
      closeCreate()
      showReveal({ operation: 'created', credential: created })
      void query.refetch()
    } catch (error) {
      setCreateError(actionErrorMessage(error))
    } finally {
      setCreating(false)
    }
  }

  const confirmAction = async () => {
    if (!active || !canManage || !pendingAction) return
    const action = pendingAction
    setActionPending(true)
    setActionError(null)
    try {
      if (action.type === 'rotate') {
        const successor = await rotateProjectCredential(
          active.projectId,
          action.credential.credential_id,
        )
        setPendingAction(null)
        showReveal({ operation: 'rotated', credential: successor })
        toast.success('Successor credential created')
      } else {
        await revokeProjectCredential(active.projectId, action.credential.credential_id)
        setPendingAction(null)
        toast.success('Credential revoked')
      }
      void query.refetch()
    } catch (error) {
      setActionError(actionErrorMessage(error))
    } finally {
      setActionPending(false)
    }
  }

  const openAudit = async (credential: CredentialMetadata) => {
    if (!active || !canManage) return
    setAudit({ credential, entries: [], loading: true, error: null })
    try {
      const entries = await getProjectCredentialAudit(active.projectId, credential.credential_id)
      setAudit((current) =>
        current?.credential.credential_id === credential.credential_id
          ? { ...current, entries, loading: false }
          : current,
      )
    } catch {
      setAudit((current) =>
        current?.credential.credential_id === credential.credential_id
          ? { ...current, loading: false, error: 'Unable to load credential audit history.' }
          : current,
      )
    }
  }

  const toggleConfidentialRole = (role: CredentialRole) => {
    setConfidentialRoles((current) =>
      current.includes(role)
        ? current.filter((item) => item !== role)
        : CREDENTIAL_ROLE_ORDER.filter((item) => item === role || current.includes(item)),
    )
  }

  if (!active) return null

  if (!canManage) {
    return (
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldAlert className="h-5 w-5" />
            SDK credentials
          </CardTitle>
          <CardDescription>
            Project <code className="font-mono">{active.projectId}</code> does not grant{' '}
            <code className="font-mono">credentials:manage</code>. Credential metadata and
            management actions remain unavailable.
          </CardDescription>
        </CardHeader>
      </Card>
    )
  }

  const credentials = query.data ?? []

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-4">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <KeyRound className="h-5 w-5" />
                SDK credentials
              </CardTitle>
              <CardDescription>
                Reveal-once browser and server credentials for active project{' '}
                <code className="font-mono">{active.projectId}</code>.
              </CardDescription>
            </div>
            <Button type="button" size="sm" onClick={() => setCreateOpen(true)}>
              <Plus />
              Create credential
            </Button>
          </div>
        </CardHeader>
        <CardContent>
          {query.isPending ? (
            <div className="flex items-center gap-2 py-8 text-sm text-muted-foreground">
              <Loader2 className="animate-spin" />
              Loading credentials…
            </div>
          ) : query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : credentials.length === 0 ? (
            <div className="rounded-md border border-dashed p-6 text-center">
              <p className="font-medium">No durable SDK credentials</p>
              <p className="mt-1 text-sm text-muted-foreground">
                Create a browser key for client instrumentation or a narrowly scoped server key.
              </p>
            </div>
          ) : (
            <div className="overflow-x-auto rounded-md border">
              <Table>
                <TableHeader>
                  <TableRow>
                    <TableHead>Credential</TableHead>
                    <TableHead>Roles</TableHead>
                    <TableHead>Status</TableHead>
                    <TableHead>Created</TableHead>
                    <TableHead className="text-right">Actions</TableHead>
                  </TableRow>
                </TableHeader>
                <TableBody>
                  {credentials.map((credential) => (
                    <TableRow key={credential.credential_id}>
                      <TableCell>
                        <div className="space-y-1">
                          <div className="flex items-center gap-2">
                            <Badge variant="outline">{credential.credential_kind}</Badge>
                            <code className="font-mono text-xs">{credential.credential_id}</code>
                          </div>
                          <p className="font-mono text-xs text-muted-foreground">
                            {credential.key_prefix}••••
                          </p>
                          {credential.rotated_from_credential_id ? (
                            <p className="text-xs text-muted-foreground">
                              rotated from{' '}
                              <code className="font-mono">
                                {credential.rotated_from_credential_id}
                              </code>
                            </p>
                          ) : null}
                        </div>
                      </TableCell>
                      <TableCell>
                        <CredentialRoles roles={credential.roles} />
                      </TableCell>
                      <TableCell>
                        <Badge variant={credential.active ? 'default' : 'secondary'}>
                          {credential.active ? 'Active' : 'Revoked'}
                        </Badge>
                      </TableCell>
                      <TableCell>
                        <RelativeTime value={credential.created_at} />
                      </TableCell>
                      <TableCell>
                        <div className="flex justify-end gap-1">
                          <Button
                            type="button"
                            variant="ghost"
                            size="sm"
                            aria-label={`View audit for ${credential.credential_id}`}
                            onClick={() => void openAudit(credential)}
                          >
                            <History />
                            Audit
                          </Button>
                          {credential.active &&
                          !credentials.some(
                            (item) =>
                              item.rotated_from_credential_id === credential.credential_id,
                          ) ? (
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              aria-label={`Rotate ${credential.credential_id}`}
                              onClick={() => {
                                setActionError(null)
                                setPendingAction({ type: 'rotate', credential })
                              }}
                            >
                              <RefreshCw />
                              Rotate
                            </Button>
                          ) : null}
                          {credential.active ? (
                            <Button
                              type="button"
                              variant="ghost"
                              size="sm"
                              className="text-destructive hover:text-destructive"
                              aria-label={`Revoke ${credential.credential_id}`}
                              onClick={() => {
                                setActionError(null)
                                setPendingAction({ type: 'revoke', credential })
                              }}
                            >
                              <Trash2 />
                              Revoke
                            </Button>
                          ) : null}
                        </div>
                      </TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </div>
          )}
        </CardContent>
      </Card>

      <Dialog open={createOpen} onOpenChange={(open) => (open ? setCreateOpen(true) : closeCreate())}>
        <DialogContent>
          <form onSubmit={(event) => void submitCreate(event)} className="space-y-5">
            <DialogHeader>
              <DialogTitle>Create SDK credential</DialogTitle>
              <DialogDescription>
                The API key is displayed once. Choose the narrowest scope your integration needs.
              </DialogDescription>
            </DialogHeader>
            <div className="space-y-1.5">
              <Label htmlFor="credential-kind">Credential type</Label>
              <Select
                id="credential-kind"
                value={kind}
                disabled={creating}
                onChange={(event) => {
                  setKind(event.target.value as CredentialKind)
                  setCreateError(null)
                }}
              >
                <option value="browser">Browser SDK</option>
                <option value="confidential">Server SDK</option>
              </Select>
            </div>
            {kind === 'browser' ? (
              <div className="space-y-2">
                <p className="text-sm font-medium">Fixed browser scope</p>
                <CredentialRoles roles={BROWSER_CREDENTIAL_ROLES} />
                <p className="text-xs text-muted-foreground">
                  Browser keys can only publish events and read client-visible configuration.
                </p>
                {!browserScopeAvailable ? (
                  <p className="text-sm text-destructive">
                    Your membership must include both fixed browser roles before you can create this
                    credential.
                  </p>
                ) : null}
              </div>
            ) : (
              <fieldset className="space-y-3">
                <legend className="text-sm font-medium">Server roles</legend>
                {CREDENTIAL_ROLE_ORDER.map((role) => (
                  <label key={role} className="flex items-start gap-3 rounded-md border p-3">
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={confidentialRoles.includes(role)}
                      disabled={creating || !hasWorkspaceRole(active, role)}
                      onChange={() => toggleConfidentialRole(role)}
                    />
                    <span>
                      <span className="block font-mono text-sm">{role}</span>
                      <span className="block text-xs text-muted-foreground">
                        {ROLE_DESCRIPTIONS[role]}
                        {!hasWorkspaceRole(active, role) ? ' · Not granted by your membership' : ''}
                      </span>
                    </span>
                  </label>
                ))}
              </fieldset>
            )}
            {createError ? <p className="text-sm text-destructive">{createError}</p> : null}
            <DialogFooter>
              <Button type="button" variant="outline" disabled={creating} onClick={closeCreate}>
                Cancel
              </Button>
              <Button
                type="submit"
                disabled={
                  creating ||
                  (kind === 'browser' && !browserScopeAvailable) ||
                  (kind === 'confidential' && selectedConfidentialRoles.length === 0)
                }
              >
                {creating ? <Loader2 className="animate-spin" /> : null}
                Create and reveal
              </Button>
            </DialogFooter>
          </form>
        </DialogContent>
      </Dialog>

      <Dialog
        open={pendingAction !== null}
        onOpenChange={(open) => {
          if (!open && !actionPending) {
            setPendingAction(null)
            setActionError(null)
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>
              {pendingAction?.type === 'rotate' ? 'Create successor credential?' : 'Revoke credential?'}
            </DialogTitle>
            <DialogDescription>
              {pendingAction?.type === 'rotate'
                ? 'Rotation creates a new credential with the same kind and roles. The current credential remains active so you can migrate safely, then revoke it separately.'
                : 'Revocation takes effect immediately. Applications still using this credential will lose access.'}
            </DialogDescription>
          </DialogHeader>
          {actionError ? <p className="text-sm text-destructive">{actionError}</p> : null}
          <DialogFooter>
            <Button
              type="button"
              variant="outline"
              disabled={actionPending}
              onClick={() => setPendingAction(null)}
            >
              Cancel
            </Button>
            <Button
              type="button"
              variant={pendingAction?.type === 'revoke' ? 'destructive' : 'default'}
              disabled={actionPending}
              onClick={() => void confirmAction()}
            >
              {actionPending ? <Loader2 className="animate-spin" /> : null}
              {pendingAction?.type === 'rotate' ? 'Create successor' : 'Revoke credential'}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <SecretRevealDialog reveal={reveal} onClose={clearReveal} />
      <AuditDialog state={audit} onClose={() => setAudit(null)} />
    </>
  )
}
