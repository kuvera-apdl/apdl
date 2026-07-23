// Steer — change how the loop behaves (admin-console-purpose-ia.md).
// Loop configuration only (autonomy, schedule, repo, custom agents); console
// and deployment operation live under System. A hub of the levers, each
// linking to its detailed surface.
import { Bot, GitBranch, Play, SlidersHorizontal } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import { PageHeader } from '@/components/shared/PageHeader'
import { Card, CardContent } from '@/components/ui/card'
import { hasWorkspaceRole, useWorkspace } from '@/core/workspace'
import { AgentReadOnlyNote } from '@/features/agents/AgentAccessNotice'

interface Lever {
  icon: LucideIcon
  title: string
  description: string
  to: string
  cta: string
}

const LEVERS: Lever[] = [
  {
    icon: Play,
    title: 'Run the loop',
    description:
      'Trigger a run and choose which agents participate, the analysis window, and the autonomy level for this run.',
    to: '/agents/trigger',
    cta: 'Configure a run',
  },
  {
    icon: Bot,
    title: 'Custom agents',
    description:
      'Define read-only analysis agents with their own prompts and query tools that run alongside the built-in loop.',
    to: '/agents/custom',
    cta: 'Manage agents',
  },
  {
    icon: GitBranch,
    title: 'Connected repository',
    description:
      'The repository the loop opens treatment and feature pull requests against. Connect, disconnect, or switch repos.',
    to: '/codegen',
    cta: 'Manage connection',
  },
]

export function SteerPage() {
  const { active } = useWorkspace()
  const canRun = hasWorkspaceRole(active, 'agents:run')
  const canManageAgents = hasWorkspaceRole(active, 'agents:manage')
  const levers = LEVERS.filter((lever) => lever.to !== '/agents/trigger' || canRun).map((lever) =>
    lever.to === '/agents/custom' && !canManageAgents
      ? {
          ...lever,
          description: 'Inspect the custom-agent definitions used by historical operator runs.',
          cta: 'View agents',
        }
      : lever,
  )

  return (
    <div className="space-y-4">
      <PageHeader
        title="Steer"
        description={
          canRun
            ? 'Change how autonomous the loop is, what it runs over, and which agents take part.'
            : 'Inspect loop configuration and agent definitions available to this read-only workspace.'
        }
      />

      {canRun ? (
        <Card>
          <CardContent className="flex items-start gap-3 p-4">
            <SlidersHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
            <div className="space-y-1 text-sm">
              <p className="font-medium">Autonomy is set per run</p>
              <p className="text-muted-foreground">
                L1 suggests only · L2 holds every passing action for approval · L3/L4 become
                eligible for automatic actions only when the operator enables autonomous
                mutations. Feature proposals always ask you, at every level. Pick the available
                level when you start a run.
              </p>
            </div>
          </CardContent>
        </Card>
      ) : (
        <AgentReadOnlyNote>
          Starting or configuring agent execution requires agents:run. Read-only run history and
          definitions remain available.
        </AgentReadOnlyNote>
      )}

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {levers.map((lever) => (
          <Link key={lever.to} to={lever.to}>
            <Card className="h-full transition-colors hover:border-foreground/20">
              <CardContent className="space-y-2 p-4">
                <lever.icon className="h-5 w-5 text-muted-foreground" />
                <p className="text-sm font-medium">{lever.title}</p>
                <p className="text-sm text-muted-foreground">{lever.description}</p>
                <p className="pt-1 text-sm text-foreground">{lever.cta} →</p>
              </CardContent>
            </Card>
          </Link>
        ))}
      </div>

      {canRun ? (
        <p className="text-xs text-muted-foreground">
          A scheduled-evaluation setting will live here once the agents service runs the loop on a
          cron; today, schedule runs from your own scheduler against POST /v1/agents/trigger.
        </p>
      ) : null}
    </div>
  )
}
