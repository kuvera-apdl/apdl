import { useQuery } from '@tanstack/react-query'
import {
  ArrowRightLeft,
  Crown,
  History,
  Loader2,
  Pencil,
  ShieldCheck,
  Trash2,
  UserPlus,
  Users,
} from 'lucide-react'
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react'
import { toast } from 'sonner'

import type { AdminRole } from '@/api/auth'
import { ApiError } from '@/api/http'
import {
  HUMAN_ROLE_ORDER,
  createProjectInvitation,
  getProjectAuthorization,
  invitationCreateRequestSchema,
  listMembershipAudit,
  listOwnershipAudit,
  listProjectMembers,
  memberRolesReplaceRequestSchema,
  removeProjectMember,
  replaceProjectMemberRoles,
  revokeProjectInvitation,
  transferProjectOwnership,
  type ProjectInvitationReveal,
  type ProjectMember,
} from '@/api/members'
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
import { useAuth } from '@/core/auth'
import { queryKeys } from '@/core/queryClient'
import { hasWorkspaceRole, useWorkspace } from '@/core/workspace'

const ROLE_LABELS: Record<AdminRole, string> = {
  'events:write': 'Publish events',
  'config:read': 'Read configuration',
  'config:write': 'Change flags and experiments',
  'config:evaluate': 'Evaluate flags',
  'query:read': 'Run analytics',
  'agents:read': 'View agent activity',
  'agents:run': 'Run agents',
  'agents:manage': 'Manage agents',
  'agents:approve': 'Approve agent actions',
  'credentials:manage': 'Manage SDK credentials',
  'members:manage': 'Manage project members',
}

function roleError(error: unknown): string {
  if (error instanceof ApiError && error.status === 403) {
    return 'Your live project authority no longer permits this action. Refresh your access.'
  }
  if (error instanceof ApiError && (error.status === 409 || error.status === 404)) {
    return error.message
  }
  return 'The member operation failed. Try again shortly.'
}

function RoleBadges({ roles }: { roles: readonly AdminRole[] }) {
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

function RoleSelector({
  allowed,
  selected,
  onChange,
  disabled,
}: {
  allowed: readonly AdminRole[]
  selected: readonly AdminRole[]
  onChange: (roles: AdminRole[]) => void
  disabled?: boolean
}) {
  const toggle = (role: AdminRole, checked: boolean) => {
    const values = new Set(selected)
    if (checked) values.add(role)
    else values.delete(role)
    onChange(HUMAN_ROLE_ORDER.filter((candidate) => values.has(candidate)))
  }
  return (
    <div className="grid gap-2 sm:grid-cols-2">
      {HUMAN_ROLE_ORDER.map((role) => {
        const available = allowed.includes(role)
        return (
          <label
            key={role}
            className="flex items-start gap-2 rounded-md border p-2.5 text-sm has-[:disabled]:opacity-50"
          >
            <input
              type="checkbox"
              className="mt-0.5"
              checked={selected.includes(role)}
              disabled={disabled || !available}
              onChange={(event) => toggle(role, event.target.checked)}
            />
            <span>
              <span className="block font-mono text-xs">{role}</span>
              <span className="text-xs text-muted-foreground">{ROLE_LABELS[role]}</span>
            </span>
          </label>
        )
      })}
    </div>
  )
}

export function ProjectMembersCard() {
  const { active } = useWorkspace()
  const { identity } = useAuth()
  const canManage = hasWorkspaceRole(active, 'members:manage')
  const [inviteOpen, setInviteOpen] = useState(false)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRoles, setInviteRoles] = useState<AdminRole[]>([])
  const [inviteReveal, setInviteReveal] = useState<ProjectInvitationReveal | null>(null)
  const revealRef = useRef<ProjectInvitationReveal | null>(null)
  const [editMember, setEditMember] = useState<ProjectMember | null>(null)
  const [editRoles, setEditRoles] = useState<AdminRole[]>([])
  const [transferOpen, setTransferOpen] = useState(false)
  const [transferTarget, setTransferTarget] = useState('')
  const [pending, setPending] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)

  const authorization = useQuery({
    queryKey: active ? queryKeys.projectAuthorization(active.id) : ['none', 'authorization'],
    enabled: active !== null,
    queryFn: ({ signal }) => getProjectAuthorization(active!.projectId, signal),
  })
  const members = useQuery({
    queryKey: active ? queryKeys.projectMembers(active.id) : ['none', 'members'],
    enabled: active !== null && canManage,
    queryFn: ({ signal }) => listProjectMembers(active!.projectId, signal),
  })
  const audit = useQuery({
    queryKey: active ? queryKeys.membershipAudit(active.id) : ['none', 'membership-audit'],
    enabled: active !== null && canManage,
    queryFn: ({ signal }) => listMembershipAudit(active!.projectId, signal),
  })
  const ownershipAudit = useQuery({
    queryKey: active ? queryKeys.ownershipAudit(active.id) : ['none', 'ownership-audit'],
    enabled: active !== null && canManage,
    queryFn: ({ signal }) => listOwnershipAudit(active!.projectId, signal),
  })

  const clearReveal = useCallback(() => {
    revealRef.current = null
    setInviteReveal(null)
  }, [])

  useEffect(() => {
    clearReveal()
    setInviteOpen(false)
    setEditMember(null)
    setTransferOpen(false)
    setActionError(null)
  }, [active?.id, clearReveal])

  useEffect(
    () => () => {
      revealRef.current = null
    },
    [],
  )

  if (!active) return null

  const isOwner =
    authorization.data?.ownership.kind === 'human' &&
    authorization.data.ownership.owner_user_id === identity?.user_id
  const availableInviteRoles = HUMAN_ROLE_ORDER.filter(
    (role) =>
      active.roles.includes(role) && (isOwner || role !== 'members:manage'),
  )
  const eligibleOwners =
    members.data?.members.filter(
      (member) =>
        member.active &&
        !member.is_owner &&
        member.roles.includes('members:manage'),
    ) ?? []

  const refreshManagement = () => {
    void members.refetch()
    void audit.refetch()
    void ownershipAudit.refetch()
  }

  const closeInvite = () => {
    clearReveal()
    setInviteOpen(false)
    setInviteEmail('')
    setInviteRoles([])
    setActionError(null)
  }

  const submitInvite = async (event: FormEvent) => {
    event.preventDefault()
    const parsed = invitationCreateRequestSchema.safeParse({
      email: inviteEmail,
      roles: inviteRoles,
    })
    if (!parsed.success) {
      setActionError(
        parsed.error.issues[0]?.message === 'roles must be unique and use canonical order'
          ? 'Select at least one role within your current authority.'
          : (parsed.error.issues[0]?.message ?? 'Enter a valid invitation'),
      )
      return
    }
    setPending(true)
    setActionError(null)
    try {
      const reveal = await createProjectInvitation(
        active.projectId,
        parsed.data.email,
        parsed.data.roles,
      )
      revealRef.current = reveal
      setInviteReveal(reveal)
      refreshManagement()
    } catch (error) {
      setActionError(roleError(error))
    } finally {
      setPending(false)
    }
  }

  const saveRoles = async () => {
    if (!editMember) return
    const parsed = memberRolesReplaceRequestSchema.safeParse({ roles: editRoles })
    if (!parsed.success) {
      setActionError('Select at least one role within your current authority.')
      return
    }
    setPending(true)
    setActionError(null)
    try {
      await replaceProjectMemberRoles(
        active.projectId,
        editMember.user_id,
        parsed.data.roles,
      )
      setEditMember(null)
      toast.success(`Roles updated for ${editMember.email}`)
      refreshManagement()
    } catch (error) {
      setActionError(roleError(error))
    } finally {
      setPending(false)
    }
  }

  const removeMember = async (member: ProjectMember) => {
    if (!window.confirm(`Remove ${member.email} from project ${active.projectId}?`)) return
    setPending(true)
    setActionError(null)
    try {
      await removeProjectMember(active.projectId, member.user_id)
      toast.success(`${member.email} removed`)
      refreshManagement()
    } catch (error) {
      setActionError(roleError(error))
    } finally {
      setPending(false)
    }
  }

  const revokeInvitation = async (invitationId: string, email: string) => {
    if (!window.confirm(`Revoke the pending invitation for ${email}?`)) return
    setPending(true)
    setActionError(null)
    try {
      await revokeProjectInvitation(active.projectId, invitationId)
      toast.success(`Invitation for ${email} revoked`)
      refreshManagement()
    } catch (error) {
      setActionError(roleError(error))
    } finally {
      setPending(false)
    }
  }

  const transferOwnership = async () => {
    if (!transferTarget) {
      setActionError('Select an eligible project manager.')
      return
    }
    setPending(true)
    setActionError(null)
    try {
      await transferProjectOwnership(active.projectId, transferTarget)
      setTransferOpen(false)
      setTransferTarget('')
      toast.success('Project ownership transferred')
      void authorization.refetch()
      refreshManagement()
    } catch (error) {
      setActionError(roleError(error))
    } finally {
      setPending(false)
    }
  }

  return (
    <>
      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5" />
            Project Authority
          </CardTitle>
          <CardDescription>
            Human ownership and creator provenance are separate from operator-controlled execution
            authorization.
          </CardDescription>
        </CardHeader>
        <CardContent>
          {authorization.isPending ? (
            <p className="flex items-center gap-2 py-4 text-sm text-muted-foreground" role="status">
              <Loader2 className="animate-spin" /> Loading project authority…
            </p>
          ) : authorization.error ? (
            <ErrorState error={authorization.error} onRetry={() => void authorization.refetch()} />
          ) : authorization.data ? (
            <div className="grid gap-3 md:grid-cols-3">
              <div className="rounded-md border p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Owner
                </p>
                {authorization.data.ownership.kind === 'human' ? (
                  <p className="mt-2 break-all text-sm font-medium">
                    {authorization.data.ownership.owner_email}
                  </p>
                ) : (
                  <>
                    <Badge variant="outline" className="mt-2">
                      Operator managed
                    </Badge>
                    <p className="mt-2 text-xs text-muted-foreground">
                      No console claim action is available.
                    </p>
                  </>
                )}
              </div>
              <div className="rounded-md border p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Created by
                </p>
                <p className="mt-2 break-all text-sm">
                  {authorization.data.creator?.email ?? 'Operator provisioning'}
                </p>
              </div>
              <div className="rounded-md border p-3">
                <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
                  Agent execution
                </p>
                <p className="mt-2 text-sm font-medium">
                  {authorization.data.execution_authorization.authorized
                    ? 'Authorized'
                    : 'Not authorized'}
                </p>
                <p className="mt-1 font-mono text-xs text-muted-foreground">
                  {authorization.data.execution_authorization.source ?? 'No authorization source'}
                </p>
                <p className="mt-2 text-xs text-muted-foreground">
                  Read-only here; ownership never changes this state.
                </p>
              </div>
            </div>
          ) : null}
        </CardContent>
      </Card>

      <Card>
        <CardHeader>
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div>
              <CardTitle className="flex items-center gap-2">
                <Users className="h-5 w-5" />
                Members
              </CardTitle>
              <CardDescription className="mt-1.5">
                Active users, pending invitations, and immutable access history for{' '}
                <span className="font-mono">{active.projectId}</span>.
              </CardDescription>
            </div>
            {canManage ? (
              <div className="flex gap-2">
                {isOwner ? (
                  <Button variant="outline" onClick={() => setTransferOpen(true)}>
                    <ArrowRightLeft />
                    Transfer ownership
                  </Button>
                ) : null}
                <Button onClick={() => setInviteOpen(true)}>
                  <UserPlus />
                  Invite member
                </Button>
              </div>
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="space-y-6">
          {!canManage ? (
            <div className="rounded-md border border-dashed p-4 text-sm text-muted-foreground">
              Member controls and access history require <code>members:manage</code>. Your project
              authority summary remains visible above.
            </div>
          ) : members.isPending ? (
            <p className="flex items-center gap-2 py-8 text-sm text-muted-foreground" role="status">
              <Loader2 className="animate-spin" /> Loading members…
            </p>
          ) : members.error ? (
            <ErrorState error={members.error} onRetry={() => void members.refetch()} />
          ) : members.data ? (
            <>
              <div className="overflow-x-auto rounded-md border">
                <Table>
                  <TableHeader>
                    <TableRow>
                      <TableHead>User</TableHead>
                      <TableHead>Roles</TableHead>
                      <TableHead>Status</TableHead>
                      <TableHead className="text-right">Actions</TableHead>
                    </TableRow>
                  </TableHeader>
                  <TableBody>
                    {members.data.members.map((member) => {
                      const delegatedCannotManage =
                        !isOwner && member.roles.includes('members:manage')
                      return (
                        <TableRow key={member.user_id}>
                          <TableCell>
                            <div className="flex items-center gap-2 font-medium">
                              {member.email}
                              {member.is_owner ? (
                                <Badge variant="outline" className="gap-1">
                                  <Crown className="h-3 w-3" /> owner
                                </Badge>
                              ) : null}
                            </div>
                            <p className="mt-1 text-xs text-muted-foreground">
                              Joined <RelativeTime value={member.joined_at} />
                            </p>
                          </TableCell>
                          <TableCell className="max-w-md">
                            <RoleBadges roles={member.roles} />
                          </TableCell>
                          <TableCell>
                            <Badge variant={member.active ? 'secondary' : 'outline'}>
                              {member.active ? 'active' : 'inactive'}
                            </Badge>
                          </TableCell>
                          <TableCell>
                            <div className="flex justify-end gap-1">
                              {!member.is_owner && !delegatedCannotManage ? (
                                <>
                                  <Button
                                    size="icon"
                                    variant="ghost"
                                    title={`Edit roles for ${member.email}`}
                                    aria-label={`Edit roles for ${member.email}`}
                                    disabled={pending}
                                    onClick={() => {
                                      setEditMember(member)
                                      setEditRoles([...member.roles])
                                      setActionError(null)
                                    }}
                                  >
                                    <Pencil />
                                  </Button>
                                  <Button
                                    size="icon"
                                    variant="ghost"
                                    title={`Remove ${member.email}`}
                                    aria-label={`Remove ${member.email}`}
                                    disabled={pending}
                                    onClick={() => void removeMember(member)}
                                  >
                                    <Trash2 />
                                  </Button>
                                </>
                              ) : (
                                <span className="text-xs text-muted-foreground">
                                  {member.is_owner ? 'Transfer first' : 'Owner managed'}
                                </span>
                              )}
                            </div>
                          </TableCell>
                        </TableRow>
                      )
                    })}
                  </TableBody>
                </Table>
              </div>

              <section className="space-y-3">
                <h3 className="font-medium">Pending invitations</h3>
                {members.data.pending_invitations.length === 0 ? (
                  <p className="text-sm text-muted-foreground">No pending invitations.</p>
                ) : (
                  <div className="space-y-2">
                    {members.data.pending_invitations.map((invitation) => (
                      <div
                        key={invitation.invitation_id}
                        className="flex flex-wrap items-center justify-between gap-3 rounded-md border p-3"
                      >
                        <div>
                          <p className="text-sm font-medium">{invitation.email}</p>
                          <p className="mt-1 text-xs text-muted-foreground">
                            Invited by {invitation.inviter_email} · expires{' '}
                            <RelativeTime value={invitation.expires_at} />
                          </p>
                          <div className="mt-2">
                            <RoleBadges roles={invitation.roles} />
                          </div>
                        </div>
                        {(isOwner || !invitation.roles.includes('members:manage')) ? (
                          <Button
                            size="sm"
                            variant="outline"
                            disabled={pending}
                            onClick={() =>
                              void revokeInvitation(invitation.invitation_id, invitation.email)
                            }
                          >
                            Revoke
                          </Button>
                        ) : (
                          <span className="text-xs text-muted-foreground">Owner managed</span>
                        )}
                      </div>
                    ))}
                  </div>
                )}
              </section>

              <section className="space-y-3">
                <h3 className="flex items-center gap-2 font-medium">
                  <History className="h-4 w-4" />
                  Recent ownership and access activity
                </h3>
                {audit.isPending || ownershipAudit.isPending ? (
                  <p className="text-sm text-muted-foreground">Loading audit history…</p>
                ) : audit.error || ownershipAudit.error ? (
                  <p className="text-sm text-destructive">
                    {(audit.error ?? ownershipAudit.error)?.message}
                  </p>
                ) : audit.data?.length || ownershipAudit.data?.length ? (
                  <div className="max-h-72 space-y-2 overflow-y-auto">
                    {ownershipAudit.data?.map((entry) => (
                      <div key={entry.audit_id} className="rounded-md border p-3 text-sm">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <Badge variant="outline">ownership transfer</Badge>
                          <RelativeTime value={entry.created_at} className="text-muted-foreground" />
                        </div>
                        <p className="mt-2">
                          {entry.previous_owner_email ?? 'Operator managed'} →{' '}
                          {entry.new_owner_email}
                        </p>
                        <p className="mt-1 text-xs text-muted-foreground">
                          {entry.actor} · {entry.reason}
                        </p>
                      </div>
                    ))}
                    {audit.data?.map((entry) => (
                      <div key={entry.audit_id} className="rounded-md border p-3 text-sm">
                        <div className="flex flex-wrap items-center justify-between gap-2">
                          <Badge variant="outline">{entry.action.replaceAll('_', ' ')}</Badge>
                          <RelativeTime value={entry.created_at} className="text-muted-foreground" />
                        </div>
                        <p className="mt-2">
                          {entry.actor_email} → {entry.subject_email}
                        </p>
                        <div className="mt-2 flex flex-wrap items-center gap-2">
                          {entry.previous_roles ? <RoleBadges roles={entry.previous_roles} /> : null}
                          {entry.previous_roles && entry.new_roles ? (
                            <span className="text-muted-foreground">→</span>
                          ) : null}
                          {entry.new_roles ? <RoleBadges roles={entry.new_roles} /> : null}
                        </div>
                      </div>
                    ))}
                  </div>
                ) : (
                  <p className="text-sm text-muted-foreground">No access changes recorded yet.</p>
                )}
              </section>
            </>
          ) : null}
          {actionError && !inviteOpen && !editMember && !transferOpen ? (
            <p className="text-sm text-destructive" role="alert">
              {actionError}
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Dialog
        open={inviteOpen}
        onOpenChange={(open) => {
          if (open) setInviteOpen(true)
          else closeInvite()
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>{inviteReveal ? 'Copy this invitation now' : 'Invite a member'}</DialogTitle>
            <DialogDescription>
              {inviteReveal
                ? 'The secure invitation URL is shown once and is cleared when this dialog closes.'
                : 'Role grants are limited to your live role ceiling. Invitations expire after seven days.'}
            </DialogDescription>
          </DialogHeader>
          {inviteReveal ? (
            <div className="space-y-4">
              <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
                Share this URL only with {inviteReveal.email}. It will not appear in invitation
                lists or browser storage.
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="invitation-url">Invitation URL</Label>
                <div className="flex gap-2">
                  <Input
                    id="invitation-url"
                    value={inviteReveal.invitation_url}
                    readOnly
                    autoComplete="off"
                    spellCheck={false}
                    className="font-mono text-xs"
                  />
                  <CopyButton
                    value={inviteReveal.invitation_url}
                    label="Copy invitation URL"
                    className="h-9 w-9 rounded-md border"
                  />
                </div>
              </div>
              <RoleBadges roles={inviteReveal.roles} />
            </div>
          ) : (
            <form onSubmit={(event) => void submitInvite(event)} className="space-y-4" noValidate>
              <div className="space-y-1.5">
                <Label htmlFor="invite-email">Email</Label>
                <Input
                  id="invite-email"
                  type="email"
                  autoComplete="off"
                  value={inviteEmail}
                  onChange={(event) => setInviteEmail(event.target.value)}
                  disabled={pending}
                />
              </div>
              <RoleSelector
                allowed={availableInviteRoles}
                selected={inviteRoles}
                onChange={setInviteRoles}
                disabled={pending}
              />
              <DialogFooter>
                <Button type="submit" disabled={pending}>
                  {pending ? <Loader2 className="animate-spin" /> : null}
                  Create invitation
                </Button>
              </DialogFooter>
            </form>
          )}
          {actionError ? (
            <p className="text-sm text-destructive" role="alert">
              {actionError}
            </p>
          ) : null}
          {inviteReveal ? (
            <DialogFooter>
              <Button onClick={closeInvite}>I have saved the invitation</Button>
            </DialogFooter>
          ) : null}
        </DialogContent>
      </Dialog>

      <Dialog
        open={editMember !== null}
        onOpenChange={(open) => {
          if (!open) {
            setEditMember(null)
            setActionError(null)
          }
        }}
      >
        <DialogContent className="max-w-2xl">
          <DialogHeader>
            <DialogTitle>Replace member roles</DialogTitle>
            <DialogDescription>
              Review the complete before and after role set for {editMember?.email}. Saving replaces
              the entire current set.
            </DialogDescription>
          </DialogHeader>
          {editMember ? (
            <div className="space-y-4">
              <div className="rounded-md border p-3 text-sm">
                <p className="mb-2 text-xs font-medium uppercase text-muted-foreground">Current</p>
                <RoleBadges roles={editMember.roles} />
              </div>
              <RoleSelector
                allowed={HUMAN_ROLE_ORDER.filter((role) => active.roles.includes(role))}
                selected={editRoles}
                onChange={setEditRoles}
                disabled={pending}
              />
            </div>
          ) : null}
          {actionError ? (
            <p className="text-sm text-destructive" role="alert">
              {actionError}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setEditMember(null)} disabled={pending}>
              Cancel
            </Button>
            <Button onClick={() => void saveRoles()} disabled={pending}>
              {pending ? <Loader2 className="animate-spin" /> : null}
              Replace role set
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>

      <Dialog
        open={transferOpen}
        onOpenChange={(open) => {
          setTransferOpen(open)
          if (!open) {
            setTransferTarget('')
            setActionError(null)
          }
        }}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Transfer project ownership</DialogTitle>
            <DialogDescription>
              Ownership moves immediately. Creator provenance and agent execution authorization do
              not change.
            </DialogDescription>
          </DialogHeader>
          <div className="space-y-1.5">
            <Label htmlFor="ownership-target">Eligible active manager</Label>
            <Select
              id="ownership-target"
              value={transferTarget}
              onChange={(event) => setTransferTarget(event.target.value)}
              disabled={pending}
            >
              <option value="">Select a manager</option>
              {eligibleOwners.map((member) => (
                <option key={member.user_id} value={member.user_id}>
                  {member.email}
                </option>
              ))}
            </Select>
            {eligibleOwners.length === 0 ? (
              <p className="text-xs text-muted-foreground">
                Grant <code>members:manage</code> to an active member before transferring.
              </p>
            ) : null}
          </div>
          <div className="rounded-md border border-amber-500/40 bg-amber-500/10 p-3 text-sm">
            Confirming makes the selected manager the only owner of project {active.projectId}.
          </div>
          {actionError ? (
            <p className="text-sm text-destructive" role="alert">
              {actionError}
            </p>
          ) : null}
          <DialogFooter>
            <Button variant="outline" onClick={() => setTransferOpen(false)} disabled={pending}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              onClick={() => void transferOwnership()}
              disabled={pending || !transferTarget}
            >
              {pending ? <Loader2 className="animate-spin" /> : null}
              Confirm transfer
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
