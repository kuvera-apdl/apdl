import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Github, Loader2, RefreshCw } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'

import {
  completeGitHubRepositoryAuthorization,
  getGitHubRepositoryAuthorization,
  getRepoConnection,
  startGitHubRepositoryAuthorization,
} from '@/api/codegen'
import { ApiError } from '@/api/http'
import { getProjectAuthorization } from '@/api/members'
import {
  githubRepositoryAuthorizationIdSchema,
  githubRepositoryCallbackStatusSchema,
  githubRepositoryProjectIdSchema,
} from '@/api/schemas/codegen'
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
import { Skeleton } from '@/components/ui/skeleton'
import { useAuth } from '@/core/auth'
import { queryKeys } from '@/core/queryClient'
import {
  hasWorkspaceRole,
  serviceConnection,
  useWorkspace,
  type Workspace,
} from '@/core/workspace'

const AUTHORIZATION_PARAM = 'github_repository_authorization'
const AUTHORIZATION_PROJECT_PARAM = 'github_repository_project_id'
const AUTHORIZATION_STATUS_PARAM = 'github_repository_status'
const AUTHORIZATION_ERROR_PARAM = 'github_repository_error'
const AUTHORIZATION_FAILED = 'authorization_failed'
const INSTALLATION_APPROVAL_REQUIRED = 'installation_approval_required'
const CALLBACK_PARAMS = [
  AUTHORIZATION_PARAM,
  AUTHORIZATION_PROJECT_PARAM,
  AUTHORIZATION_STATUS_PARAM,
  AUTHORIZATION_ERROR_PARAM,
] as const

interface GitHubConnectionCardProps {
  /** Explicit navigation seam for isolated component tests. */
  redirectToInstallation?: (url: string) => void
}

function defaultRedirectToInstallation(url: string): void {
  window.location.assign(url)
}

function safeConnectionError(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.code === 'schema_mismatch') {
      return 'GitHub returned an invalid repository authorization response. No connection was changed.'
    }
    if (error.status === 403) {
      return 'Repository connection changes require project ownership or delegated management access.'
    }
    if (error.status === 404 || error.status === 410) {
      return 'This GitHub repository authorization is unavailable or expired. Start again.'
    }
    if (error.status === 409) {
      return 'The repository authorization changed before it could be completed. Refresh and try again.'
    }
    if ([502, 503, 504].includes(error.status)) {
      return 'GitHub authorization is temporarily unavailable. No connection was changed.'
    }
  }
  return 'The GitHub repository connection could not be changed. Try again shortly.'
}

function pendingStatusMessage(status: 'awaiting_installation' | 'awaiting_oauth'): string {
  return status === 'awaiting_installation'
    ? 'GitHub is still confirming the App installation for this project.'
    : 'GitHub is still confirming the user authorization for this project.'
}

export function GitHubConnectionCard({
  redirectToInstallation = defaultRedirectToInstallation,
}: GitHubConnectionCardProps = {}) {
  const { active, workspaces, setActive } = useWorkspace()
  const { identity, initializing } = useAuth()
  const queryClient = useQueryClient()
  const [searchParams, setSearchParams] = useSearchParams()
  const ws = active as Workspace
  const projectId = active?.projectId ?? ''

  const [authorizationId, setAuthorizationId] = useState<string | null>(null)
  const [authorizationProjectId, setAuthorizationProjectId] = useState<string | null>(null)
  const [approvalRequiredProjectId, setApprovalRequiredProjectId] = useState<string | null>(null)
  const [selectedCandidateId, setSelectedCandidateId] = useState<string | null>(null)
  const [dialogError, setDialogError] = useState<string | null>(null)
  const [callbackFailed, setCallbackFailed] = useState(false)
  const handledCallbackRef = useRef<string | null>(null)
  const previousWorkspaceIdRef = useRef<string | null>(active?.id ?? null)

  const callbackValues = CALLBACK_PARAMS.map((name) => [name, searchParams.getAll(name)] as const)
  const callbackPayloadPresent = callbackValues.some(([, values]) => values.length > 0)
  const callbackKey = JSON.stringify(callbackValues)

  useEffect(() => {
    if (!callbackPayloadPresent) {
      handledCallbackRef.current = null
      return
    }
    if (initializing || handledCallbackRef.current === callbackKey) return
    handledCallbackRef.current = callbackKey

    const authorizationValues = searchParams.getAll(AUTHORIZATION_PARAM)
    const projectValues = searchParams.getAll(AUTHORIZATION_PROJECT_PARAM)
    const statusValues = searchParams.getAll(AUTHORIZATION_STATUS_PARAM)
    const errorValues = searchParams.getAll(AUTHORIZATION_ERROR_PARAM)

    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        for (const name of CALLBACK_PARAMS) next.delete(name)
        return next
      },
      { replace: true },
    )

    setAuthorizationId(null)
    setAuthorizationProjectId(null)
    setApprovalRequiredProjectId(null)
    setSelectedCandidateId(null)
    setDialogError(null)

    if (
      errorValues.length === 1 &&
      errorValues[0] === AUTHORIZATION_FAILED &&
      authorizationValues.length === 0 &&
      projectValues.length === 0 &&
      statusValues.length === 0
    ) {
      setCallbackFailed(true)
      return
    }

    if (
      authorizationValues.length === 1 &&
      projectValues.length === 1 &&
      statusValues.length === 0 &&
      errorValues.length === 0
    ) {
      const parsedAuthorization = githubRepositoryAuthorizationIdSchema.safeParse(
        authorizationValues[0],
      )
      const parsedProject = githubRepositoryProjectIdSchema.safeParse(projectValues[0])
      const targetWorkspace = parsedProject.success
        ? workspaces.find((workspace) => workspace.projectId === parsedProject.data)
        : undefined
      if (parsedAuthorization.success && parsedProject.success && targetWorkspace) {
        setCallbackFailed(false)
        setActive(targetWorkspace.id)
        setAuthorizationProjectId(parsedProject.data)
        setAuthorizationId(parsedAuthorization.data)
        return
      }
    }

    if (
      authorizationValues.length === 0 &&
      projectValues.length === 1 &&
      statusValues.length === 1 &&
      errorValues.length === 0
    ) {
      const parsedStatus = githubRepositoryCallbackStatusSchema.safeParse(statusValues[0])
      const parsedProject = githubRepositoryProjectIdSchema.safeParse(projectValues[0])
      const targetWorkspace = parsedProject.success
        ? workspaces.find((workspace) => workspace.projectId === parsedProject.data)
        : undefined
      if (
        parsedStatus.success &&
        parsedStatus.data === INSTALLATION_APPROVAL_REQUIRED &&
        parsedProject.success &&
        targetWorkspace
      ) {
        setCallbackFailed(false)
        setActive(targetWorkspace.id)
        setApprovalRequiredProjectId(parsedProject.data)
        return
      }
    }

    setCallbackFailed(true)
  }, [
    callbackKey,
    callbackPayloadPresent,
    initializing,
    searchParams,
    setActive,
    setSearchParams,
    workspaces,
  ])

  useEffect(() => {
    const previousWorkspaceId = previousWorkspaceIdRef.current
    if (previousWorkspaceId !== null && active?.id && previousWorkspaceId !== active.id) {
      if (authorizationProjectId !== null && active.projectId !== authorizationProjectId) {
        setAuthorizationId(null)
        setAuthorizationProjectId(null)
        setSelectedCandidateId(null)
        setDialogError(null)
      }
      if (approvalRequiredProjectId !== null && active.projectId !== approvalRequiredProjectId) {
        setApprovalRequiredProjectId(null)
      }
    }
    previousWorkspaceIdRef.current = active?.id ?? null
  }, [active?.id, active?.projectId, approvalRequiredProjectId, authorizationProjectId])

  const callbackContextPending =
    callbackPayloadPresent ||
    (authorizationProjectId !== null && authorizationProjectId !== projectId) ||
    (approvalRequiredProjectId !== null && approvalRequiredProjectId !== projectId)
  const delegated =
    hasWorkspaceRole(active, 'agents:manage') &&
    hasWorkspaceRole(active, 'credentials:manage')

  const managementAuthorization = useQuery({
    queryKey: active ? queryKeys.projectAuthorization(active.id) : ['none', 'authorization'],
    enabled: active !== null && !delegated && !callbackContextPending,
    queryFn: ({ signal }) => getProjectAuthorization(active!.projectId, signal),
  })
  const owner =
    managementAuthorization.data?.ownership.kind === 'human' &&
    managementAuthorization.data.ownership.owner_user_id === identity?.user_id
  const canManage = delegated || owner

  const query = useQuery({
    queryKey: active ? queryKeys.repoConnection(active.id) : ['none', 'repo-connection'],
    enabled: active !== null && projectId !== '' && !callbackContextPending,
    queryFn: () => getRepoConnection(serviceConnection(ws, 'codegen'), projectId),
  })
  const connection = query.data ?? null

  const authorizationQuery = useQuery({
    queryKey:
      active && authorizationId
        ? queryKeys.githubRepositoryAuthorization(active.id, authorizationId)
        : ['none', 'github-repository-authorization'],
    enabled:
      active !== null &&
      canManage &&
      authorizationId !== null &&
      authorizationProjectId !== null &&
      active.projectId === authorizationProjectId,
    queryFn: ({ signal }) =>
      getGitHubRepositoryAuthorization(
        serviceConnection(ws, 'codegen'),
        projectId,
        authorizationId!,
        { signal },
      ),
    refetchInterval: (currentQuery) => {
      const status = currentQuery.state.data?.status
      return status === 'awaiting_installation' || status === 'awaiting_oauth' ? 2_000 : false
    },
  })

  useEffect(() => {
    const repositories = authorizationQuery.data?.repositories ?? []
    setSelectedCandidateId((current) => {
      if (current && repositories.some((repository) => repository.candidate_id === current)) {
        return current
      }
      return repositories.length === 1 ? repositories[0].candidate_id : null
    })
  }, [authorizationQuery.data?.repositories])

  useEffect(() => {
    if (authorizationQuery.data?.status !== 'completed' || !active) return
    void queryClient.invalidateQueries({ queryKey: queryKeys.repoConnection(active.id) })
    toast.success('GitHub repository connected')
    setAuthorizationId(null)
    setAuthorizationProjectId(null)
    setSelectedCandidateId(null)
    setDialogError(null)
  }, [active, authorizationQuery.data?.status, queryClient])

  function clearCallbackNotice(): void {
    setCallbackFailed(false)
    setApprovalRequiredProjectId(null)
    setSearchParams(
      (previous) => {
        const next = new URLSearchParams(previous)
        for (const name of CALLBACK_PARAMS) next.delete(name)
        return next
      },
      { replace: true },
    )
  }

  function closeAuthorizationDialog(): void {
    setAuthorizationId(null)
    setAuthorizationProjectId(null)
    setSelectedCandidateId(null)
    setDialogError(null)
  }

  const startAuthorization = useMutation({
    mutationFn: async () => {
      if (!active || !canManage) throw new Error('Repository connection is not authorized')
      clearCallbackNotice()
      const started = await startGitHubRepositoryAuthorization(
        serviceConnection(active, 'codegen'),
        active.projectId,
      )
      redirectToInstallation(started.installation_url)
      return started
    },
    onError: (error) => toast.error(safeConnectionError(error)),
  })

  const completeAuthorization = useMutation({
    mutationFn: async (candidateId: string) => {
      if (
        !active ||
        !authorizationId ||
        !authorizationProjectId ||
        active.projectId !== authorizationProjectId ||
        !canManage
      ) {
        throw new Error('Repository connection is not authorized')
      }
      return completeGitHubRepositoryAuthorization(
        serviceConnection(active, 'codegen'),
        active.projectId,
        authorizationId,
        candidateId,
      )
    },
    onSuccess: (saved) => {
      if (active) {
        queryClient.setQueryData(queryKeys.repoConnection(active.id), saved)
        void queryClient.invalidateQueries({ queryKey: queryKeys.repoConnection(active.id) })
      }
      toast.success(`Connected ${saved.repository_full_name}`)
      closeAuthorizationDialog()
    },
    onError: (error) => setDialogError(safeConnectionError(error)),
  })

  return (
    <>
      <Card>
        <CardHeader>
          <div className="flex items-start justify-between gap-4">
            <div className="space-y-1.5">
              <CardTitle className="flex items-center gap-2">
                <Github className="h-4 w-4" />
                GitHub repository
              </CardTitle>
              <CardDescription>
                Project-scoped repository used by Codegen for{' '}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">{projectId || '?'}</code>.
              </CardDescription>
            </div>
            {query.isSuccess ? (
              connection ? (
                <Badge className="bg-emerald-600 hover:bg-emerald-600">Connected</Badge>
              ) : (
                <Badge variant="outline">Not connected</Badge>
              )
            ) : null}
          </div>
        </CardHeader>
        <CardContent className="space-y-4">
          {approvalRequiredProjectId ? (
            <div
              role="alert"
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-amber-500/40 bg-amber-500/5 p-3 text-sm"
            >
              <span>
                A GitHub organization owner must approve the APDL GitHub App before project{' '}
                <code className="rounded bg-muted px-1 py-0.5 text-xs">
                  {approvalRequiredProjectId}
                </code>{' '}
                can connect a repository. After approval, start the connection again.
              </span>
              <div className="flex gap-2">
                {canManage ? (
                  <Button
                    size="sm"
                    onClick={() => startAuthorization.mutate()}
                    disabled={startAuthorization.isPending}
                  >
                    {startAuthorization.isPending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Github />
                    )}
                    Try again after approval
                  </Button>
                ) : null}
                <Button size="sm" variant="ghost" onClick={clearCallbackNotice}>
                  Dismiss
                </Button>
              </div>
            </div>
          ) : null}

          {callbackFailed ? (
            <div
              role="alert"
              className="flex flex-wrap items-center justify-between gap-3 rounded-md border border-destructive/40 bg-destructive/5 p-3 text-sm"
            >
              <span>GitHub could not authorize the repository. No connection was changed.</span>
              <div className="flex gap-2">
                {canManage ? (
                  <Button
                    size="sm"
                    onClick={() => startAuthorization.mutate()}
                    disabled={startAuthorization.isPending}
                  >
                    {startAuthorization.isPending ? (
                      <Loader2 className="animate-spin" />
                    ) : (
                      <Github />
                    )}
                    Try again
                  </Button>
                ) : null}
                <Button size="sm" variant="ghost" onClick={clearCallbackNotice}>
                  Dismiss
                </Button>
              </div>
            </div>
          ) : null}

          {query.isPending ? (
            <Skeleton className="h-16 w-full" />
          ) : query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : connection ? (
            <div className="flex flex-wrap items-center justify-between gap-4">
              <div className="space-y-1 text-sm">
                <p className="font-medium">{connection.repository_full_name}</p>
                <p className="text-muted-foreground">
                  base branch{' '}
                  <code className="rounded bg-muted px-1 py-0.5 text-xs">
                    {connection.default_base_branch}
                  </code>
                  {' · '}repository #{connection.repository_id}
                  {' · '}grant <code className="font-mono text-xs">{connection.grant_id}</code>
                  {' · '}connected <RelativeTime value={connection.updated_at} />
                </p>
              </div>
              {canManage ? (
                <Button
                  variant="outline"
                  onClick={() => startAuthorization.mutate()}
                  disabled={startAuthorization.isPending}
                >
                  {startAuthorization.isPending ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Github />
                  )}
                  Change repository
                </Button>
              ) : null}
            </div>
          ) : (
            <div className="flex flex-wrap items-center justify-between gap-4">
              <p className="max-w-2xl text-sm text-muted-foreground">
                Connect the APDL GitHub App and choose the repository Codegen may use for this
                project. GitHub installation authority stays server-side.
              </p>
              {canManage ? (
                <Button
                  onClick={() => startAuthorization.mutate()}
                  disabled={startAuthorization.isPending}
                >
                  {startAuthorization.isPending ? (
                    <Loader2 className="animate-spin" />
                  ) : (
                    <Github />
                  )}
                  Connect GitHub
                </Button>
              ) : null}
            </div>
          )}

          {!canManage && !managementAuthorization.isPending ? (
            <p className="text-xs text-muted-foreground">
              Connection changes require project ownership or both <code>agents:manage</code> and{' '}
              <code>credentials:manage</code>.
            </p>
          ) : null}
          {!delegated && managementAuthorization.isError ? (
            <p role="alert" className="text-xs text-destructive">
              Repository connection management access could not be verified.
            </p>
          ) : null}
        </CardContent>
      </Card>

      <Dialog
        open={authorizationId !== null}
        onOpenChange={(open) => !open && closeAuthorizationDialog()}
      >
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Choose a GitHub repository</DialogTitle>
            <DialogDescription>
              Only repositories verified for this project-scoped GitHub authorization are shown.
            </DialogDescription>
          </DialogHeader>

          <div className="space-y-3 py-2">
            {!delegated && managementAuthorization.isPending ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Verifying project management access…
              </div>
            ) : !canManage ? (
              <p role="alert" className="text-sm text-destructive">
                You do not have permission to change this project&apos;s repository connection.
              </p>
            ) : authorizationQuery.isPending ? (
              <div className="flex items-center gap-2 text-sm text-muted-foreground">
                <Loader2 className="h-4 w-4 animate-spin" />
                Loading repositories from GitHub…
              </div>
            ) : authorizationQuery.isError ? (
              <div className="space-y-3">
                <p role="alert" className="text-sm text-destructive">
                  {safeConnectionError(authorizationQuery.error)}
                </p>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => void authorizationQuery.refetch()}
                >
                  <RefreshCw /> Retry
                </Button>
              </div>
            ) : authorizationQuery.data.status === 'awaiting_selection' ? (
              authorizationQuery.data.repositories.length === 0 ? (
                <p className="text-sm text-muted-foreground">
                  This GitHub installation does not currently offer any repositories for selection.
                </p>
              ) : (
                <fieldset className="max-h-80 space-y-2 overflow-y-auto pr-1">
                  <legend className="sr-only">Repository</legend>
                  {authorizationQuery.data.repositories.map((repository) => {
                    const inputId = `github-repository-${repository.candidate_id}`
                    return (
                      <label
                        key={repository.candidate_id}
                        htmlFor={inputId}
                        className="flex cursor-pointer gap-3 rounded-md border p-3 hover:bg-muted/50"
                      >
                        <input
                          id={inputId}
                          type="radio"
                          name="github-repository-candidate"
                          value={repository.candidate_id}
                          checked={selectedCandidateId === repository.candidate_id}
                          onChange={() => {
                            setSelectedCandidateId(repository.candidate_id)
                            setDialogError(null)
                          }}
                          className="mt-1 accent-foreground"
                        />
                        <span className="min-w-0 flex-1 space-y-1">
                          <span className="flex flex-wrap items-center gap-2 font-medium">
                            {repository.repository_full_name}
                            <Badge variant="outline">
                              {repository.private ? 'Private' : 'Public'}
                            </Badge>
                          </span>
                          <span className="block text-xs text-muted-foreground">
                            base branch {repository.default_base_branch}
                            {' · '}repository #{repository.repository_id}
                          </span>
                        </span>
                      </label>
                    )
                  })}
                </fieldset>
              )
            ) : authorizationQuery.data.status === 'completed' ? (
              <p className="text-sm text-muted-foreground">Repository connection completed.</p>
            ) : (
              <div className="space-y-2">
                <p className="text-sm text-muted-foreground">
                  {pendingStatusMessage(authorizationQuery.data.status)}
                </p>
                <p className="text-xs text-muted-foreground">
                  Authorization expires <RelativeTime value={authorizationQuery.data.expires_at} />.
                </p>
              </div>
            )}

            {dialogError ? (
              <p role="alert" className="text-sm text-destructive">
                {dialogError}
              </p>
            ) : null}
          </div>

          <DialogFooter>
            <Button
              variant="outline"
              onClick={closeAuthorizationDialog}
              disabled={completeAuthorization.isPending}
            >
              Cancel
            </Button>
            {authorizationQuery.data?.status === 'awaiting_selection' ? (
              <Button
                onClick={() => {
                  if (selectedCandidateId) completeAuthorization.mutate(selectedCandidateId)
                }}
                disabled={selectedCandidateId === null || completeAuthorization.isPending}
              >
                {completeAuthorization.isPending ? <Loader2 className="animate-spin" /> : null}
                Connect repository
              </Button>
            ) : authorizationQuery.data &&
              (authorizationQuery.data.status === 'awaiting_installation' ||
                authorizationQuery.data.status === 'awaiting_oauth') ? (
              <Button
                onClick={() => void authorizationQuery.refetch()}
                disabled={authorizationQuery.isFetching}
              >
                {authorizationQuery.isFetching ? (
                  <Loader2 className="animate-spin" />
                ) : (
                  <RefreshCw />
                )}
                Refresh
              </Button>
            ) : null}
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </>
  )
}
