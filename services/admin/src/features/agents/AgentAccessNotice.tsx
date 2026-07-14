import { ShieldAlert } from 'lucide-react'
import type { ReactNode } from 'react'

import type { AdminRole } from '@/api/auth'
import { EmptyState } from '@/components/shared/PanelStates'

const ROLE_LABELS: Record<Extract<AdminRole, `agents:${string}`>, string> = {
  'agents:read': 'read agent data',
  'agents:run': 'start agent runs',
  'agents:manage': 'manage and test custom agents',
  'agents:approve': 'submit approval decisions and resume runs',
}

export function AgentRoleUnavailable({
  role,
  title,
}: {
  role: Extract<AdminRole, `agents:${string}`>
  title: string
}) {
  return (
    <EmptyState
      icon={<ShieldAlert className="h-8 w-8" />}
      title={title}
      description={`This workspace does not grant ${role}, which is required to ${ROLE_LABELS[role]}. Existing definitions, runs, results, and audit history remain read-only.`}
    />
  )
}

export function AgentReadOnlyNote({ children }: { children: ReactNode }) {
  return (
    <p className="rounded-md border bg-muted/40 px-3 py-2 text-sm text-muted-foreground">
      {children}
    </p>
  )
}
