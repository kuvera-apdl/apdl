import { useQuery } from '@tanstack/react-query'
import { ExternalLink, Github } from 'lucide-react'

import { getRepoConnection } from '@/api/codegen'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace, type Workspace } from '@/core/workspace'

export function GitHubConnectionCard() {
  const { active } = useWorkspace()
  const ws = active as Workspace
  const projectId = active?.projectId ?? ''
  const query = useQuery({
    queryKey: active ? queryKeys.repoConnection(active.id) : ['none', 'repo-connection'],
    enabled: active !== null && projectId !== '',
    queryFn: () => getRepoConnection(serviceConnection(ws, 'codegen'), projectId),
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
              Operator-managed repository binding for project{' '}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">{projectId || '?'}</code>.
            </CardDescription>
          </div>
          {query.isSuccess ? (
            connection ? (
              <Badge className="bg-emerald-600 hover:bg-emerald-600">Verified grant</Badge>
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
          <div className="space-y-1 text-sm">
            <a
              href={`https://github.com/${connection.repository_full_name}`}
              target="_blank"
              rel="noreferrer"
              className="inline-flex items-center gap-1 font-medium hover:underline"
            >
              {connection.repository_full_name}
              <ExternalLink className="h-3 w-3" />
            </a>
            <p className="text-muted-foreground">
              base branch{' '}
              <code className="rounded bg-muted px-1 py-0.5 text-xs">
                {connection.default_base_branch}
              </code>
              {' · '}repository #{connection.repository_id}
              {' · '}grant <code className="font-mono text-xs">{connection.grant_id}</code>
              {' · '}verified <RelativeTime value={connection.updated_at} />
            </p>
          </div>
        ) : (
          <p className="text-sm text-muted-foreground">
            No verified repository grant is active. An operator must authorize the exact GitHub
            repository ID before Codegen can enqueue or publish work for this project.
          </p>
        )}
      </CardContent>
    </Card>
  )
}
