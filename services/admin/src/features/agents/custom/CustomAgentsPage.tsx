// Custom agents — list/manage the project's user-defined analysis agents.
import { Archive, Pencil, Plus, Puzzle } from 'lucide-react'
import { useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { archiveCustomAgent, listCustomAgents, listCustomAgentsCurl } from '@/api/agents'
import { ApiError } from '@/api/http'
import type { CustomAgent } from '@/api/types/agents'
import { CurlButton } from '@/components/shared/CurlButton'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table'
import { queryKeys } from '@/core/queryClient'
import { serviceConnection, useWorkspace } from '@/core/workspace'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

export function CustomAgentsPage() {
  const { active, projectId } = useWorkspace()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [toArchive, setToArchive] = useState<CustomAgent | null>(null)

  const conn = active ? serviceConnection(active, 'agents') : null

  const query = useQuery({
    queryKey: active && projectId ? queryKeys.customAgents(active.id, projectId) : ['custom-agents-idle'],
    enabled: Boolean(conn && projectId),
    queryFn: ({ signal }) => listCustomAgents(conn!, projectId!, { signal }),
  })

  const archiveMutation = useMutation({
    mutationFn: (agent: CustomAgent) => archiveCustomAgent(conn!, projectId!, agent.agent_id),
    onSuccess: (_data, agent) => {
      toast.success(`Custom agent "${agent.display_name}" archived`)
      if (active) {
        void queryClient.invalidateQueries({ queryKey: queryKeys.customAgentsPrefix(active.id) })
      }
      setToArchive(null)
    },
    onError: (error) => {
      toast.error(error instanceof ApiError ? error.message : 'Archive failed')
    },
  })

  return (
    <div className="space-y-5">
      <PageHeader
        backTo={{ to: '/agents', label: 'Agent runs' }}
        title="Custom agents"
        description="Project-scoped, read-only analysis agents: your prompts plus a selection of query tools, run through the same supervisor pipeline as the built-ins. They never deploy anything."
        actions={
          <>
            {conn && projectId ? (
              <CurlButton spec={listCustomAgentsCurl(conn, projectId)} title="List custom agents" />
            ) : null}
            <Button onClick={() => navigate('/agents/custom/new')}>
              <Plus />
              New custom agent
            </Button>
          </>
        }
      />

      <Card>
        <CardContent className="p-0">
          {query.isPending ? (
            <div className="space-y-2 p-4">
              <Skeleton className="h-8 w-full" />
              <Skeleton className="h-8 w-full" />
            </div>
          ) : query.isError ? (
            <ErrorState error={query.error} onRetry={() => void query.refetch()} />
          ) : query.data.length === 0 ? (
            <EmptyState
              icon={<Puzzle className="h-8 w-8" />}
              title="No custom agents yet"
              description="Compose an analysis agent from prompts and read-only data tools; it becomes selectable when triggering a run."
            >
              <Button size="sm" onClick={() => navigate('/agents/custom/new')}>
                <Plus />
                New custom agent
              </Button>
            </EmptyState>
          ) : (
            <Table>
              <TableHeader>
                <TableRow>
                  <TableHead>Agent</TableHead>
                  <TableHead>Slug</TableHead>
                  <TableHead>Tier</TableHead>
                  <TableHead>Tools</TableHead>
                  <TableHead>Produces</TableHead>
                  <TableHead>Order</TableHead>
                  <TableHead>Updated</TableHead>
                  <TableHead className="w-24" />
                </TableRow>
              </TableHeader>
              <TableBody>
                {query.data.map((agent) => (
                  <TableRow key={agent.agent_id}>
                    <TableCell>
                      <Link
                        to={`/agents/custom/${encodeURIComponent(agent.agent_id)}/edit`}
                        className="font-medium hover:underline"
                      >
                        {agent.display_name}
                      </Link>
                      {agent.description ? (
                        <p className="max-w-xs truncate text-xs text-muted-foreground">
                          {agent.description}
                        </p>
                      ) : null}
                    </TableCell>
                    <TableCell className="font-mono text-xs">{agent.slug}</TableCell>
                    <TableCell>
                      <Badge variant="secondary">{agent.model_tier}</Badge>
                    </TableCell>
                    <TableCell className="tabular-nums">{agent.tools.length}</TableCell>
                    <TableCell className="font-mono text-xs">{agent.produces}</TableCell>
                    <TableCell className="tabular-nums">{agent.pipeline_order}</TableCell>
                    <TableCell>
                      <RelativeTime value={agent.updated_at} className="text-muted-foreground" />
                    </TableCell>
                    <TableCell>
                      <div className="flex items-center justify-end gap-1">
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Edit ${agent.display_name}`}
                          onClick={() =>
                            navigate(`/agents/custom/${encodeURIComponent(agent.agent_id)}/edit`)
                          }
                        >
                          <Pencil />
                        </Button>
                        <Button
                          variant="ghost"
                          size="icon"
                          aria-label={`Archive ${agent.display_name}`}
                          onClick={() => setToArchive(agent)}
                        >
                          <Archive />
                        </Button>
                      </div>
                    </TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </CardContent>
      </Card>

      <Dialog open={toArchive !== null} onOpenChange={(open) => !open && setToArchive(null)}>
        <DialogContent>
          <DialogHeader>
            <DialogTitle>Archive custom agent?</DialogTitle>
            <DialogDescription>
              &ldquo;{toArchive?.display_name}&rdquo; stops resolving in new and resumed runs
              immediately. Its slug becomes reusable; past run results are kept.
            </DialogDescription>
          </DialogHeader>
          <DialogFooter>
            <Button variant="outline" onClick={() => setToArchive(null)}>
              Cancel
            </Button>
            <Button
              variant="destructive"
              disabled={archiveMutation.isPending}
              onClick={() => toArchive && archiveMutation.mutate(toArchive)}
            >
              Archive
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </div>
  )
}
