// Steer — change how the loop behaves (admin-console-purpose-ia.md).
// Loop configuration only (autonomy, schedule, repo, custom agents); console
// and deployment operation live under System. A hub of the levers, each
// linking to its detailed surface.
import { Bot, GitBranch, Play, SlidersHorizontal } from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { Link } from 'react-router-dom'

import { PageHeader } from '@/components/shared/PageHeader'
import { Card, CardContent } from '@/components/ui/card'

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
  return (
    <div className="space-y-4">
      <PageHeader
        title="Steer"
        description="Change how autonomous the loop is, what it runs over, and which agents take part."
      />

      <Card>
        <CardContent className="flex items-start gap-3 p-4">
          <SlidersHorizontal className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <div className="space-y-1 text-sm">
            <p className="font-medium">Autonomy is set per run</p>
            <p className="text-muted-foreground">
              L1 suggests only · L2 auto-applies safe actions and asks you for the rest · L3 also
              auto-applies low-risk actions · L4 is full autonomy. Feature proposals always ask you,
              at every level. Pick the level when you start a run.
            </p>
          </div>
        </CardContent>
      </Card>

      <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
        {LEVERS.map((lever) => (
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

      <p className="text-xs text-muted-foreground">
        A scheduled-evaluation setting will live here once the agents service runs the loop on a
        cron; today, schedule runs from your own scheduler against POST /v1/agents/trigger.
      </p>
    </div>
  )
}
