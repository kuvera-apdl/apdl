// Read-only render of rules in evaluation order (plan §5.3.2).
import type { FlagConfig, GateCondition, GateRule } from '@/api/types/flags'
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card'
import { formatPercent } from '@/lib/format'

const EXISTENCE_OPERATORS = new Set(['exists', 'not_exists'])

function ConditionLine({ condition }: { condition: GateCondition }) {
  return (
    <span>
      <code className="font-mono text-xs">{condition.attribute}</code>{' '}
      <span className="text-muted-foreground">{condition.operator.replace(/_/g, ' ')}</span>
      {!EXISTENCE_OPERATORS.has(condition.operator) ? (
        <>
          {' '}
          <code className="font-mono text-xs">{JSON.stringify(condition.value)}</code>
        </>
      ) : null}
    </span>
  )
}

function RuleCard({ rule, index }: { rule: GateRule; index: number }) {
  return (
    <Card>
      <CardHeader className="pb-3">
        <CardTitle className="flex items-center gap-2 text-sm">
          <span className="inline-flex h-5 w-5 items-center justify-center rounded-full bg-secondary text-xs tabular-nums">
            {index + 1}
          </span>
          {rule.name || <span className="text-muted-foreground">Unnamed rule</span>}
          <code className="ml-auto font-mono text-xs text-muted-foreground">{rule.id}</code>
        </CardTitle>
      </CardHeader>
      <CardContent className="space-y-2 text-sm">
        {rule.conditions.length > 0 ? (
          <ol className="list-inside list-decimal space-y-1">
            {rule.conditions.map((condition, conditionIndex) => (
              <li key={conditionIndex}>
                <ConditionLine condition={condition} />
              </li>
            ))}
          </ol>
        ) : (
          <p className="text-muted-foreground">No conditions — matches every user.</p>
        )}
        <p className="border-t pt-2 text-xs text-muted-foreground">
          Rollout: <span className="font-medium text-foreground">{formatPercent(rule.rollout.percentage)}</span>{' '}
          of matching users, bucketed by{' '}
          <code className="font-mono">{rule.rollout.bucket_by}</code>
        </p>
      </CardContent>
    </Card>
  )
}

export function TargetingTab({ flag }: { flag: FlagConfig }) {
  return (
    <div className="max-w-3xl space-y-3">
      {flag.rules.length === 0 ? (
        <p className="text-sm text-muted-foreground">
          No targeting rules — all traffic goes straight to fallthrough.
        </p>
      ) : (
        flag.rules.map((rule, index) => <RuleCard key={rule.id} rule={rule} index={index} />)
      )}

      <Card className="border-dashed">
        <CardHeader className="pb-3">
          <CardTitle className="text-sm">Fallthrough</CardTitle>
        </CardHeader>
        <CardContent>
          <p className="text-xs text-muted-foreground">
            <span className="font-medium text-foreground">
              {formatPercent(flag.fallthrough.rollout.percentage)}
            </span>{' '}
            of remaining users, bucketed by{' '}
            <code className="font-mono">{flag.fallthrough.rollout.bucket_by}</code>
          </p>
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        Rules evaluate top-down; the first rule whose conditions all match wins. Users missing
        every rule fall through.
      </p>
    </div>
  )
}
