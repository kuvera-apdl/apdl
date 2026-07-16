import { ShieldCheck } from 'lucide-react'

import { PageHeader } from '@/components/shared/PageHeader'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { useAuth } from '@/core/auth'
import { useWorkspace } from '@/core/workspace'

export function WorkspaceSettingsPage() {
  const { identity } = useAuth()
  const { active, setActive } = useWorkspace()

  return (
    <div className="space-y-6">
      <PageHeader
        title="Project access"
        description="Projects and roles granted to your server-side administrator account."
      />

      <Card>
        <CardHeader>
          <CardTitle className="flex items-center gap-2">
            <ShieldCheck className="h-5 w-5" />
            Secure session
          </CardTitle>
          <CardDescription>
            Signed in as {identity?.email}. Authentication uses an HttpOnly session cookie; no API
            keys or internal service tokens are stored in this browser.
          </CardDescription>
        </CardHeader>
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
    </div>
  )
}
