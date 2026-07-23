import { Loader2, Plus, ShieldCheck } from 'lucide-react'
import { useState, type FormEvent } from 'react'
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
import { ProjectCredentialsCard } from '@/features/settings/ProjectCredentialsCard'

export function WorkspaceSettingsPage() {
  const { identity, createProject } = useAuth()
  const { active, setActive } = useWorkspace()
  const [projectId, setProjectId] = useState('')
  const [creating, setCreating] = useState(false)
  const [error, setError] = useState<string | null>(null)

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

  return (
    <div className="space-y-6">
      <PageHeader
        title="Workspace settings"
        description="Project access, roles, and reveal-once SDK credentials."
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5" />
            Secure session
          </CardTitle>
          <CardDescription>
            Signed in as {identity?.email}. Authentication uses an HttpOnly session cookie; no API
            keys or internal service tokens are stored persistently in this browser. Reveal-once
            keys exist only while their dialog is open.
          </CardDescription>
        </CardHeader>
      </Card>

      {identity?.projects.length === 0 ? (
        <Card>
          <CardHeader>
            <CardTitle>No project access yet</CardTitle>
            <CardDescription>
              This account is registered but has not been assigned to a project. An operator must
              grant project roles before service data becomes available.
            </CardDescription>
          </CardHeader>
        </Card>
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

      <div className="grid gap-4 md:grid-cols-2">
        {identity?.projects.map((project) => (
          <Card key={project.project_id} className={active?.id === project.project_id ? 'border-primary' : undefined}>
            <CardHeader>
              <div className="flex items-center justify-between gap-3">
                <CardTitle className="font-mono text-base">{project.project_id}</CardTitle>
                {active?.id === project.project_id ? <Badge>active</Badge> : null}
              </div>
            </CardHeader>
            <CardContent className="space-y-4">
              <div className="flex flex-wrap gap-1.5">
                {project.roles.map((role) => (
                  <Badge key={role} variant="secondary" className="font-mono text-xs">
                    {role}
                  </Badge>
                ))}
              </div>
              {active?.id !== project.project_id ? (
                <Button variant="outline" size="sm" onClick={() => setActive(project.project_id)}>
                  Activate project
                </Button>
              ) : null}
            </CardContent>
          </Card>
        ))}
      </div>

      <ProjectCredentialsCard />
    </div>
  )
}
