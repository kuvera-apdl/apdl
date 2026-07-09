// GitHub connector: shows whether the active project is bound to a repository
// (codegen /v1/connections) and lets the operator connect or disconnect one.
// Connecting is a picker over the repos the APDL GitHub App can already reach
// (codegen /v1/github/repos) — the App install happens on github.com; picking
// a repo pre-fills its default branch and the service validates the rest. If
// the listing is unavailable, a manual slug entry remains as the escape hatch
// (the service resolves + validates the installation server-side).
import { zodResolver } from '@hookform/resolvers/zod'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, Github, Loader2 } from 'lucide-react'
import { useState } from 'react'
import { useForm } from 'react-hook-form'
import { toast } from 'sonner'
import { z } from 'zod'

import { connectRepo, disconnectRepo, getRepoConnection, listAccessibleRepos } from '@/api/codegen'
import { ApiError } from '@/api/http'
import { REPO_SLUG_PATTERN } from '@/api/schemas/codegen'
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
import { Skeleton } from '@/components/ui/skeleton'
import { queryKeys } from '@/core/queryClient'
import { projectIdFromKey, serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'

const connectFormSchema = z.object({
  repo: z.string().regex(REPO_SLUG_PATTERN, 'Format: owner/name'),
  baseBranch: z.string().min(1, 'Required'),
})

type ConnectFormValues = z.infer<typeof connectFormSchema>

export function GitHubConnectionCard() {
  const { active } = useWorkspace()
  const queryClient = useQueryClient()
  // Rendered behind RequireAuth, so `active` is non-null.
  const ws = active as Workspace
  const projectId = active ? (projectIdFromKey(active.apiKey) ?? '') : ''
  const [confirmingDisconnect, setConfirmingDisconnect] = useState(false)

  const query = useQuery({
    queryKey: active ? queryKeys.repoConnection(active.id) : ['none', 'repo-connection'],
    enabled: active !== null && projectId !== '',
    queryFn: () => getRepoConnection(serviceConnection(ws, 'codegen'), ws.internalToken, projectId),
  })

  // The picker's options — only fetched while the connect form is showing.
  const reposQuery = useQuery({
    queryKey: active ? queryKeys.accessibleRepos(active.id) : ['none', 'github-repos'],
    enabled: active !== null && query.isSuccess && query.data === null,
    queryFn: ({ signal }) =>
      listAccessibleRepos(serviceConnection(ws, 'codegen'), ws.internalToken, { signal }),
  })

  const form = useForm<ConnectFormValues>({
    resolver: zodResolver(connectFormSchema),
    defaultValues: { repo: '', baseBranch: 'main' },
  })

  const invalidate = () => {
    if (active) {
      void queryClient.invalidateQueries({ queryKey: queryKeys.repoConnection(active.id) })
    }
  }
  const onError = (fallback: string) => (error: Error) =>
    toast.error(error instanceof ApiError ? error.message : fallback)

  const connect = useMutation({
    // When the repo came from the picker its installation id rides along; a
    // manually-typed slug omits it and the service resolves (and validates) it.
    mutationFn: (values: ConnectFormValues) =>
      connectRepo(serviceConnection(ws, 'codegen'), ws.internalToken, {
        project_id: projectId,
        installation_id: reposQuery.data?.find((r) => r.repo === values.repo)?.installation_id,
        repo: values.repo,
        default_base_branch: values.baseBranch,
      }),
    onSuccess: (connection) => {
      toast.success(`Connected to ${connection.repo}`)
      form.reset()
      invalidate()
    },
    onError: onError('Connect failed'),
  })

  const disconnect = useMutation({
    mutationFn: () => disconnectRepo(serviceConnection(ws, 'codegen'), ws.internalToken, projectId),
    onSuccess: () => {
      toast.success('Repository disconnected')
      setConfirmingDisconnect(false)
      invalidate()
    },
    onError: (error: Error) => {
      setConfirmingDisconnect(false)
      onError('Disconnect failed')(error)
    },
  })

  const connection = query.data ?? null

  return (
    <Card>
      <CardHeader>
        <div className="flex items-start justify-between gap-4">
          <div className="space-y-1.5">
            <CardTitle className="flex items-center gap-2">
              <Github className="h-4 w-4" />
              GitHub repository
            </CardTitle>
            <CardDescription>
              The repo the autonomous loop opens pull requests on for project{' '}
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
      <CardContent>
        {query.isPending ? (
          <Skeleton className="h-16 w-full" />
        ) : query.isError ? (
          <ErrorState error={query.error} onRetry={() => void query.refetch()} />
        ) : connection ? (
          <div className="flex flex-wrap items-center justify-between gap-4">
            <div className="space-y-1 text-sm">
              <a
                href={`https://github.com/${connection.repo}`}
                target="_blank"
                rel="noreferrer"
                className="inline-flex items-center gap-1 font-medium hover:underline"
              >
                {connection.repo}
                <ExternalLink className="h-3 w-3" />
              </a>
              <p className="text-muted-foreground">
                base branch <code className="rounded bg-muted px-1 py-0.5 text-xs">{connection.default_base_branch}</code>
                {' · '}installation #{connection.installation_id}
                {' · '}connected <RelativeTime value={connection.updated_at} />
              </p>
            </div>
            <Button variant="outline" onClick={() => setConfirmingDisconnect(true)}>
              Disconnect
            </Button>
          </div>
        ) : reposQuery.isPending ? (
          <Skeleton className="h-16 w-full" />
        ) : (
          <form
            onSubmit={form.handleSubmit((values) => connect.mutate(values))}
            className="space-y-4"
            noValidate
          >
            {reposQuery.isError ? (
              <p className="text-sm text-muted-foreground">
                Couldn't list the App's repositories (
                {reposQuery.error instanceof ApiError
                  ? reposQuery.error.message
                  : 'request failed'}
                ). Enter the repository manually — the install is validated on connect.
              </p>
            ) : reposQuery.data.length === 0 ? (
              <p className="text-sm text-muted-foreground">
                The APDL GitHub App has no repository access yet. Install it on the target
                repository on github.com, then reload — or enter the repository manually.
              </p>
            ) : (
              <p className="text-sm text-muted-foreground">
                Pick one of the {reposQuery.data.length} repositories the APDL GitHub App can
                reach. Missing one? Install the App on it on github.com first.
              </p>
            )}
            <div className="grid gap-4 sm:grid-cols-2">
              <div className="space-y-1.5">
                <Label htmlFor="connect-repo">Repository</Label>
                {reposQuery.isSuccess && reposQuery.data.length > 0 ? (
                  <Select
                    id="connect-repo"
                    className="font-mono"
                    {...form.register('repo', {
                      // Picking a repo pre-fills its default branch.
                      onChange: (event: React.ChangeEvent<HTMLSelectElement>) => {
                        const picked = reposQuery.data.find((r) => r.repo === event.target.value)
                        if (picked) form.setValue('baseBranch', picked.default_branch)
                      },
                    })}
                  >
                    <option value="">Select a repository…</option>
                    {reposQuery.data.map((repo) => (
                      <option key={repo.repo} value={repo.repo}>
                        {repo.repo}
                        {repo.private ? ' (private)' : ''}
                      </option>
                    ))}
                  </Select>
                ) : (
                  <Input
                    id="connect-repo"
                    {...form.register('repo')}
                    placeholder="owner/name"
                    className="font-mono"
                  />
                )}
                {form.formState.errors.repo ? (
                  <p className="text-xs text-destructive">{form.formState.errors.repo.message}</p>
                ) : null}
              </div>
              <div className="space-y-1.5">
                <Label htmlFor="connect-base-branch">Base branch</Label>
                <Input
                  id="connect-base-branch"
                  {...form.register('baseBranch')}
                  placeholder="main"
                />
                {form.formState.errors.baseBranch ? (
                  <p className="text-xs text-destructive">
                    {form.formState.errors.baseBranch.message}
                  </p>
                ) : null}
              </div>
            </div>
            <Button type="submit" disabled={connect.isPending}>
              {connect.isPending ? <Loader2 className="animate-spin" /> : null}
              Connect repository
            </Button>
          </form>
        )}
      </CardContent>

      <Dialog open={confirmingDisconnect} onOpenChange={setConfirmingDisconnect}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Disconnect repository?</DialogTitle>
            <DialogDescription>
              Removes the binding between project "{projectId}" and {connection?.repo}. The
              autonomous loop stops opening pull requests; existing changesets and open PRs are
              untouched. The GitHub App installation itself is managed on github.com.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setConfirmingDisconnect(false)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={disconnect.isPending}
              onClick={() => disconnect.mutate()}
            >
              {disconnect.isPending ? <Loader2 className="animate-spin" /> : null}
              Disconnect
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </Card>
  )
}
