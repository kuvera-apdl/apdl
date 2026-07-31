import { Bot, ChevronDown, FolderKanban, KeyRound, Loader2, Plus, ShieldCheck } from 'lucide-react'
import { useEffect, useState, type FormEvent } from 'react'
import { useSearchParams } from 'react-router-dom'
import { toast } from 'sonner'
import { z } from 'zod'

import { ApiError } from '@/api/http'
import { PageHeader } from '@/components/shared/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { useAuth } from '@/core/auth'
import { useWorkspace } from '@/core/workspace'
import { AgenticRunsCard } from '@/features/agents/setup/AgenticRunsCard'
import { ProjectCredentialsCard } from '@/features/settings/ProjectCredentialsCard'
import { ProjectLlmConnectionsCard } from '@/features/settings/ProjectLlmConnectionsCard'
import { ProjectMembersCard } from '@/features/settings/ProjectMembersCard'

export function WorkspaceSettingsPage() {
  const { identity, createProject } = useAuth()
  const { active, setActive } = useWorkspace()
  const [searchParams, setSearchParams] = useSearchParams()
  const [expandedProjectId, setExpandedProjectId] = useState<string | null>(null)
  const [newProjectSetupId, setNewProjectSetupId] = useState<string | null>(
    null,
  )
  const [projectId, setProjectId] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    setExpandedProjectId(active?.id ?? null)
  }, [active?.id])

  const onCreateProject = async (event: FormEvent) => {
    event.preventDefault()
    const parsed = z
      .string()
      .regex(/^[A-Za-z0-9]{1,64}$/, 'Use 1–64 letters or numbers')
      .safeParse(projectId)
    if (!parsed.success) {
      setError(parsed.error.issues[0]?.message ?? 'Invalid project ID')
      return
    }
    setCreating(true)
    setError(null)
    try {
      await createProject(parsed.data)
      setActive(parsed.data)
      setExpandedProjectId(parsed.data)
      setNewProjectSetupId(parsed.data)
      setProjectId('')
      toast.success(`Project "${parsed.data}" created`)
    } catch (caught) {
      if (caught instanceof ApiError && caught.code === 'project_quota_reached') {
        setError('This account has reached its project limit. Ask an operator for access.')
      } else if (caught instanceof ApiError && caught.status === 409) {
        setError('That project ID already exists.')
      } else {
        setError('Unable to create the project. Try again shortly.')
      }
    } finally {
      setCreating(false)
    }
  }

  const toggleProject = (nextProjectId: string) => {
    if (expandedProjectId === nextProjectId) {
      setExpandedProjectId(null)
      return
    }
    setActive(nextProjectId)
    setExpandedProjectId(nextProjectId)
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Project management"
        description="Review your projects, manage access, connect Agents providers, and issue reveal-once SDK credentials."
      />

      {identity?.projects.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>No project access yet</CardTitle>
            <CardDescription>
              This account starts empty. Create your first project below to receive the core roles
              needed to configure it and issue SDK credentials.
            </CardDescription>
          </CardHeader>
        </Card>
      ) : null}

      {identity?.projects.length ? (
        <section aria-labelledby="projects-heading" className="space-y-3">
          <div>
            <h2 id="projects-heading" className="text-base font-semibold">
              Projects
            </h2>
            <p className="mt-1 text-sm text-muted-foreground">
              Select a project to review its authority, collaborators, and integration access.
            </p>
          </div>

          <div className="space-y-4">
            {identity.projects.map((project) => {
              const expanded =
                expandedProjectId === project.project_id && active?.id === project.project_id
              const contentId = `project-${project.project_id}-management`

              return (
                <Card key={project.project_id} className="w-full overflow-hidden">
                  <button
                    type="button"
                    className="flex w-full items-center gap-4 p-5 text-left transition-colors hover:bg-muted/40"
                    aria-expanded={expanded}
                    aria-controls={contentId}
                    onClick={() => toggleProject(project.project_id)}
                  >
                    <span className="flex h-10 w-10 shrink-0 items-center justify-center rounded-md bg-muted">
                      <FolderKanban className="h-5 w-5 text-muted-foreground" />
                    </span>
                    <span className="min-w-0 flex-1">
                      <span className="flex flex-wrap items-center gap-2">
                        <span className="truncate font-mono font-semibold">
                          {project.project_id}
                        </span>
                        {active?.id === project.project_id ? <Badge>active project</Badge> : null}
                      </span>
                      <span className="mt-1 block text-sm text-muted-foreground">
                        {project.roles.length}{' '}
                        {project.roles.length === 1 ? 'permission' : 'permissions'} ·{' '}
                        {identity.email}
                      </span>
                    </span>
                    <ChevronDown
                      className={`h-5 w-5 shrink-0 text-muted-foreground transition-transform ${
                        expanded ? 'rotate-180' : ''
                      }`}
                    />
                  </button>

                  {expanded ? (
                    <div
                      id={contentId}
                      className="space-y-4 border-t bg-muted/20 p-4 md:p-5"
                    >
                      <Card>
                        <CardHeader>
                          <CardTitle className="flex items-center gap-2">
                            <ShieldCheck className="h-5 w-5" />
                            Your Access
                          </CardTitle>
                          <CardDescription>
                            Signed in as {identity.email}. These permissions apply only to project{' '}
                            <span className="font-mono">{project.project_id}</span>.
                          </CardDescription>
                        </CardHeader>
                        <CardContent>
                          <div className="flex flex-wrap gap-1.5">
                            {project.roles.map((role) => (
                              <Badge key={role} variant="secondary" className="font-mono text-xs">
                                {role}
                              </Badge>
                            ))}
                          </div>
                        </CardContent>
                      </Card>

                      <ProjectMembersCard />

                      <section
                        aria-labelledby={`project-${project.project_id}-agents-heading`}
                        className="space-y-3"
                      >
                        <div>
                          <h3
                            id={`project-${project.project_id}-agents-heading`}
                            className="flex items-center gap-2 font-semibold"
                          >
                            <Bot className="h-4 w-4" />
                            Agents
                          </h3>
                          <p className="mt-1 text-sm text-muted-foreground">
                            Connect the project credentials Agents uses for provider calls and
                            discover the models available to each key.
                          </p>
                        </div>
                        <ProjectLlmConnectionsCard />
                      </section>

                      <section
                        aria-labelledby={`project-${project.project_id}-sdk-heading`}
                        className="space-y-3"
                      >
                        <div>
                          <h3
                            id={`project-${project.project_id}-sdk-heading`}
                            className="flex items-center gap-2 font-semibold"
                          >
                            <KeyRound className="h-4 w-4" />
                            SDK connections
                          </h3>
                          <p className="mt-1 text-sm text-muted-foreground">
                            Issue scoped reveal-once keys for browser and server SDKs.
                          </p>
                        </div>
                        <ProjectCredentialsCard />
                      </section>
                      <AgenticRunsCard
                        autoOpen={
                          newProjectSetupId === project.project_id ||
                          (active?.id === project.project_id &&
                            searchParams.get('agents_setup') === '1')
                        }
                        onAutoOpenHandled={() => {
                          setNewProjectSetupId(null)
                          if (searchParams.get('agents_setup') === '1') {
                            const next = new URLSearchParams(searchParams)
                            next.delete('agents_setup')
                            setSearchParams(next, { replace: true })
                          }
                        }}
                      />
                    </div>
                  ) : null}
                </Card>
              )
            })}
          </div>
        </section>
      ) : null}

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <Plus className="h-5 w-5" />
            Create project
          </CardTitle>
          <CardDescription>
            Create a project and associate it with this account. You receive the project roles
            needed to configure and operate it.
          </CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={(event) => void onCreateProject(event)} className="space-y-4" noValidate>
            <div className="space-y-1.5">
              <Label htmlFor="new-project-id">Project ID</Label>
              <Input
                id="new-project-id"
                className="font-mono"
                value={projectId}
                onChange={(event) => setProjectId(event.target.value)}
                placeholder="myproject"
                disabled={creating}
              />
              <p className="text-xs text-muted-foreground">
                1–64 letters or numbers. Project IDs are permanent.
              </p>
            </div>
            {error ? <p className="text-sm text-destructive">{error}</p> : null}
            <Button type="submit" disabled={creating}>
              {creating ? <Loader2 className="animate-spin" /> : null}
              Create project
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  )
}
