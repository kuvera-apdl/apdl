// Population simulator (plan §5.3.6): run N synthetic unit ids through the
// parity-tested local evaluator and show where traffic enters (rules vs
// fallthrough vs nothing) and the observed variant split vs configured
// weights. All simulated users share the provided attributes; only the unit
// id varies.
import { useMemo } from 'react'

import { evaluateFlag, type EvaluableFlag } from '@/core/evaluator/evaluate'
import { formatPercent } from '@/lib/format'
import { cn } from '@/lib/utils'

const POPULATION = 10_000

interface RuleEntry {
  id: string
  label: string
  assigned: number
  missed: number
}

interface SimulationSummary {
  population: number
  rules: RuleEntry[]
  fallthroughAssigned: number
  fallthroughMissed: number
  unassigned: number
  assignedTotal: number
  variants: { key: string; observed: number; configuredShare: number }[]
}

function simulate(flag: EvaluableFlag, attributes: Record<string, unknown>): SimulationSummary {
  const ruleStats = new Map<string, { assigned: number; missed: number }>()
  for (const rule of flag.rules) ruleStats.set(rule.id, { assigned: 0, missed: 0 })
  const variantCounts = new Map<string, number>(flag.variants.map((variant) => [variant.key, 0]))

  let fallthroughAssigned = 0
  let fallthroughMissed = 0
  let unassigned = 0

  for (let index = 0; index < POPULATION; index++) {
    const unit = `user_${index}`
    const result = evaluateFlag(flag, { user_id: unit, anonymous_id: unit, attributes })
    if (result.reason === 'rule_match' && result.rule_id) {
      ruleStats.get(result.rule_id)!.assigned += 1
      if (result.variant) variantCounts.set(result.variant, (variantCounts.get(result.variant) ?? 0) + 1)
    } else if (result.reason === 'rule_rollout' && result.rule_id) {
      ruleStats.get(result.rule_id)!.missed += 1
    } else if (result.reason === 'fallthrough') {
      fallthroughAssigned += 1
      if (result.variant) variantCounts.set(result.variant, (variantCounts.get(result.variant) ?? 0) + 1)
    } else if (result.reason === 'fallthrough_rollout') {
      fallthroughMissed += 1
    } else {
      unassigned += 1
    }
  }

  const totalWeight = flag.variants.reduce((sum, variant) => sum + variant.weight, 0)
  const assignedTotal =
    fallthroughAssigned + [...ruleStats.values()].reduce((sum, stat) => sum + stat.assigned, 0)

  return {
    population: POPULATION,
    rules: flag.rules.map((rule, index) => ({
      id: rule.id,
      label: rule.name || `Rule ${index + 1}`,
      assigned: ruleStats.get(rule.id)?.assigned ?? 0,
      missed: ruleStats.get(rule.id)?.missed ?? 0,
    })),
    fallthroughAssigned,
    fallthroughMissed,
    unassigned,
    assignedTotal,
    variants: flag.variants.map((variant) => ({
      key: variant.key,
      observed: variantCounts.get(variant.key) ?? 0,
      configuredShare: totalWeight > 0 ? (variant.weight / totalWeight) * 100 : 0,
    })),
  }
}

function ShareBar({ label, count, population, tone }: { label: string; count: number; population: number; tone: string }) {
  const share = (count / population) * 100
  return (
    <div className="space-y-1">
      <div className="flex items-baseline justify-between gap-2 text-sm">
        <span className="truncate">{label}</span>
        <span className="shrink-0 tabular-nums text-muted-foreground">
          {formatPercent(share)} · {count.toLocaleString()}
        </span>
      </div>
      <div className="h-2 w-full overflow-hidden rounded-full bg-muted">
        <div className={cn('h-full', tone)} style={{ width: `${share}%` }} />
      </div>
    </div>
  )
}

interface PopulationSimulatorProps {
  flag: EvaluableFlag
  attributes?: Record<string, unknown>
}

export function PopulationSimulator({ flag, attributes = {} }: PopulationSimulatorProps) {
  // Key by content — callers rebuild the flag object every render.
  const memoKey = JSON.stringify([flag, attributes])
  const summary = useMemo(() => simulate(flag, attributes), [memoKey])

  return (
    <div className="space-y-5">
      <div className="space-y-2.5">
        <p className="text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Entry path — {summary.population.toLocaleString()} simulated users
        </p>
        {summary.rules.map((rule) => (
          <div key={rule.id} className="space-y-1.5">
            <ShareBar
              label={`${rule.label} — assigned`}
              count={rule.assigned}
              population={summary.population}
              tone="bg-sky-500"
            />
            {rule.missed > 0 ? (
              <ShareBar
                label={`${rule.label} — matched but missed rollout`}
                count={rule.missed}
                population={summary.population}
                tone="bg-sky-500/40"
              />
            ) : null}
          </div>
        ))}
        <ShareBar
          label="Fallthrough — assigned"
          count={summary.fallthroughAssigned}
          population={summary.population}
          tone="bg-emerald-500"
        />
        {summary.fallthroughMissed > 0 ? (
          <ShareBar
            label="Fallthrough — missed rollout"
            count={summary.fallthroughMissed}
            population={summary.population}
            tone="bg-emerald-500/40"
          />
        ) : null}
        {summary.unassigned > 0 ? (
          <ShareBar
            label="No assignment (disabled / missing unit id)"
            count={summary.unassigned}
            population={summary.population}
            tone="bg-muted-foreground/50"
          />
        ) : null}
      </div>

      <div>
        <p className="mb-2 text-xs font-medium uppercase tracking-wide text-muted-foreground">
          Variant split among {summary.assignedTotal.toLocaleString()} assigned users
        </p>
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b text-left text-xs text-muted-foreground">
              <th className="py-1.5 font-medium">Variant</th>
              <th className="py-1.5 text-right font-medium">Observed</th>
              <th className="py-1.5 text-right font-medium">Observed %</th>
              <th className="py-1.5 text-right font-medium">Configured %</th>
            </tr>
          </thead>
          <tbody>
            {summary.variants.map((variant) => (
              <tr key={variant.key} className="border-b last:border-0">
                <td className="py-1.5 font-mono text-xs">{variant.key}</td>
                <td className="py-1.5 text-right tabular-nums">{variant.observed.toLocaleString()}</td>
                <td className="py-1.5 text-right tabular-nums">
                  {summary.assignedTotal > 0
                    ? formatPercent((variant.observed / summary.assignedTotal) * 100)
                    : '—'}
                </td>
                <td className="py-1.5 text-right tabular-nums text-muted-foreground">
                  {formatPercent(variant.configuredShare)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
        <p className="mt-2 text-xs text-muted-foreground">
          Expected sampling error at this population is roughly ±1 percentage point — observed
          shares within that of configured weights are healthy.
        </p>
      </div>
    </div>
  )
}
