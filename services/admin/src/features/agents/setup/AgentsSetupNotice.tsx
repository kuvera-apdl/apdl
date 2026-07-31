import { Bot, Settings2 } from 'lucide-react'
import { Link } from 'react-router-dom'

import type { AgentsSetup } from '@/api/types/agents-setup'
import { EmptyState } from '@/components/shared/PanelStates'
import { Button } from '@/components/ui/button'

export function AgentsSetupNotice({
  setup,
  title = 'Agentic runs are not active',
}: {
  setup: AgentsSetup
  title?: string
}) {
  const blocked = setup.state === 'active' && !setup.analysis_ready
  const canManage = setup.caller_capabilities.can_manage
  return (
    <EmptyState
      icon={<Bot className="h-8 w-8" />}
      title={blocked ? 'Agentic runs need attention' : title}
      description={
        blocked
          ? 'The current model assignments or provider connections no longer pass admission checks. Review setup before starting another run.'
          : canManage
            ? 'Connect an LLM provider and assign current fast and reasoning models before starting an agent run.'
            : 'A project owner or delegated Agents manager must connect a provider, assign models, and activate this project.'
      }
    >
      {canManage ? (
        <Button size="sm" asChild>
          <Link to="/settings/workspace?agents_setup=1">
            <Settings2 />
            {blocked ? 'Review Agents setup' : 'Set up Agentic runs'}
          </Link>
        </Button>
      ) : null}
    </EmptyState>
  )
}
