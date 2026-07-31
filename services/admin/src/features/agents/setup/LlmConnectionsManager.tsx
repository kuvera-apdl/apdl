import { KeyRound, Loader2 } from 'lucide-react'

import type { LlmProvider } from '@/api/types/agents-setup'
import { ErrorState } from '@/components/shared/PanelStates'
import { RelativeTime } from '@/components/shared/RelativeTime'
import { Badge } from '@/components/ui/badge'
import { useAgentsConnections } from '@/features/agents/setup/hooks'

const PROVIDER_LABELS: Record<LlmProvider, string> = {
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  google: 'Google',
  xai: 'xAI',
}

export function LlmConnectionsManager({
  canManage,
  assignedProviders = [],
}: {
  canManage: boolean
  onChanged?: () => void
  assignedProviders?: readonly LlmProvider[]
}) {
  const connectionsQuery = useAgentsConnections()
  const connections = connectionsQuery.data?.connections ?? []

  return (
    <div className="space-y-4">
      <div>
        <h4 className="text-sm font-semibold">Agents provider projections</h4>
        <p className="mt-1 text-xs text-muted-foreground">
          Credential lifecycle is managed centrally in Project settings. This view shows only
          connections currently granted to Agents.
        </p>
      </div>

      {connectionsQuery.isPending ? (
        <div className="flex items-center gap-2 rounded-md border p-4 text-sm text-muted-foreground">
          <Loader2 className="h-4 w-4 animate-spin" /> Loading provider projections…
        </div>
      ) : connectionsQuery.isError ? (
        <ErrorState
          error={connectionsQuery.error}
          onRetry={() => void connectionsQuery.refetch()}
        />
      ) : connections.length === 0 ? (
        <div className="rounded-md border border-dashed p-5 text-center">
          <KeyRound className="mx-auto h-6 w-6 text-muted-foreground" />
          <p className="mt-2 text-sm font-medium">No credential is granted to Agents</p>
          <p className="mt-1 text-xs text-muted-foreground">
            {canManage
              ? 'Add or update a connection in Project settings, then return here to assign models.'
              : 'A project credential manager must grant a vault connection to Agents.'}
          </p>
        </div>
      ) : (
        <div className="divide-y rounded-md border">
          {connections.map((connection) => (
            <div
              key={connection.provider}
              className="flex flex-wrap items-center justify-between gap-3 p-3"
            >
              <div>
                <div className="flex flex-wrap items-center gap-2">
                  <span className="text-sm font-medium">
                    {PROVIDER_LABELS[connection.provider]}
                  </span>
                  <Badge variant={connection.state === 'active' ? 'default' : 'secondary'}>
                    {connection.state}
                  </Badge>
                  {assignedProviders.includes(connection.provider) ? (
                    <Badge variant="outline">assigned</Badge>
                  ) : null}
                </div>
                <p className="mt-1 text-xs text-muted-foreground">
                  Version {connection.version} · {connection.model_count} models · validated{' '}
                  <RelativeTime value={connection.validated_at} />
                </p>
              </div>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
