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
            description="Guardrails are read-only diagnostics in the OSS developer preview."
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
        Guardrail results are read-only in the OSS developer preview. Automatic flag disabling is
        intentionally unavailable until the analytics decision contract is release-ready.
      </p>
    </div>
  )
}
