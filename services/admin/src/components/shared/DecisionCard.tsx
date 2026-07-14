import type { LucideIcon } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { EvidenceRow, type Evidence } from '@/components/shared/EvidenceRow'
import { LoopStatusPill } from '@/components/shared/LoopStatusPill'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import type { LoopStage } from '@/lib/loopStatus'
import { cn } from '@/lib/utils'

export interface DecisionAction {
  label: string
  onClick: () => void
  variant?: 'default' | 'outline' | 'destructive'
  disabled?: boolean
}

// A single pending decision, phrased as a question with its evidence inline
// (admin-console-purpose-ia.md → Decide). The reusable unit of the Decide
// surface: whatever the gate (design approval, ship verdict, PR merge), it
// renders the same — icon, question, stage, evidence, one line of detail,
// and the accept/reject actions. `emphasis` accents the freshest / most
// consequential decision.
export function DecisionCard({
  icon: Icon,
  question,
  stage,
  evidence,
  detail,
  actions,
  detailLink,
  emphasis,
}: {
  icon: LucideIcon
  question: ReactNode
  stage: LoopStage
  evidence?: Evidence[]
  detail?: ReactNode
  actions: DecisionAction[]
  detailLink?: { to: string; label: string }
  emphasis?: boolean
}) {
  return (
    <Card className={cn(emphasis && 'border-foreground/20')}>
      <CardContent className="space-y-2.5 p-4">
        <div className="flex items-start gap-2.5">
          <Icon className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
          <p className="min-w-0 flex-1 text-sm font-medium">{question}</p>
          <LoopStatusPill stage={stage} />
        </div>
        {evidence && evidence.length > 0 ? <EvidenceRow items={evidence} className="pl-7" /> : null}
        {detail ? <p className="pl-7 text-sm text-muted-foreground">{detail}</p> : null}
        <div className="flex flex-wrap items-center gap-2 pl-7">
          {actions.map((action) => (
            <Button
              key={action.label}
              size="sm"
              variant={action.variant ?? 'outline'}
              disabled={action.disabled}
              onClick={action.onClick}
            >
              {action.label}
            </Button>
          ))}
          {detailLink ? (
            <Link
              to={detailLink.to}
              className="ml-auto text-sm text-muted-foreground underline-offset-4 hover:text-foreground hover:underline"
            >
              {detailLink.label}
            </Link>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}
