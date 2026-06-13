import { ShieldCheck } from 'lucide-react'

import type { FlagConfig } from '@/api/types/flags'
import { EmptyState } from '@/components/shared/PanelStates'
import { Table, TableBody, TableCell, TableHead, TableHeader, TableRow } from '@/components/ui/table'

export function GuardrailsTab({ flag }: { flag: FlagConfig }) {
  return (
    <div className="max-w-3xl space-y-3">
      {flag.guardrails.length === 0 ? (
        <div className="rounded-lg border">
          <EmptyState
            icon={<ShieldCheck className="h-8 w-8" />}
            title="No guardrails configured"
            description="Guardrails auto-disable the flag when an error metric breaches its threshold."
          />
        </div>
      ) : (
        <div className="rounded-lg border">
          <Table>
            <TableHeader>
              <TableRow>
                <TableHead>Metric</TableHead>
                <TableHead>Threshold</TableHead>
                <TableHead>Scope</TableHead>
                <TableHead>Min. exposures</TableHead>
                <TableHead>Window</TableHead>
              </TableRow>
            </TableHeader>
            <TableBody>
              {flag.guardrails.map((guardrail, index) => (
                <TableRow key={index}>
                  <TableCell>
                    <code className="font-mono text-xs">{guardrail.metric}</code>
                  </TableCell>
                  <TableCell>
                    <code className="font-mono text-xs">{guardrail.threshold}</code>
                  </TableCell>
                  <TableCell>
                    {guardrail.scope || <span className="text-muted-foreground">project-wide</span>}
                  </TableCell>
                  <TableCell className="tabular-nums">{guardrail.minimum_exposures}</TableCell>
                  <TableCell className="tabular-nums">{guardrail.window_minutes} min</TableCell>
                </TableRow>
              ))}
            </TableBody>
          </Table>
        </div>
      )}
      <p className="text-xs text-muted-foreground">
        Breaches are enforced by the config service's background monitor (every 60s when the
        server runs with <code className="font-mono">GUARDRAIL_MONITOR_ENABLED</code>). Whether the
        monitor is currently enabled is not exposed by the API yet — see plan gap G6.
      </p>
    </div>
  )
}
