// Learn — recent behavior-analysis insights with links into Analytics.
import { useQueries, useQuery } from '@tanstack/react-query'
import { BarChart3, Lightbulb } from 'lucide-react'
import { useMemo } from 'react'
import { Link } from 'react-router-dom'

import { listRuns, runResults } from '@/api/agents'
import { ApiError } from '@/api/http'
import type { RunResults } from '@/api/types/agents'
import { EvidenceRow, type Evidence } from '@/components/shared/EvidenceRow'
import { PageHeader } from '@/components/shared/PageHeader'
import { EmptyState, ErrorState } from '@/components/shared/PanelStates'
import { SectionHeading } from '@/components/shared/SectionHeading'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { Skeleton } from '@/components/ui/skeleton'
import { serviceConnection, useWorkspace } from '@/core/workspace'

interface Insight {
  title: string
  description: string
  confidence?: string
  impact?: string
  recommended_action?: string
}

function asInsight(raw: unknown): Insight | null {
  if (typeof raw !== 'object' || raw === null) return null
  const rec = raw as Record<string, unknown>
  const title = typeof rec.title === 'string' ? rec.title : ''
  if (!title) return null
  return {
    title,
    description: typeof rec.description === 'string' ? rec.description : '',
    confidence: typeof rec.confidence === 'string' ? rec.confidence : undefined,
    impact: typeof rec.impact === 'string' ? rec.impact : undefined,
    recommended_action:
      typeof rec.recommended_action === 'string' ? rec.recommended_action : undefined,
  }
}

export function LearnPage() {
  const { active, projectId } = useWorkspace()
  const conn = active ? serviceConnection(active, 'agents') : null

  const runsQuery = useQuery({
    queryKey: [active?.id ?? 'none', 'learn', 'runs'],
    enabled: Boolean(conn && projectId),
    queryFn: ({ signal }) => listRuns(conn!, { projectId: projectId!, limit: 20 }, { signal }),
  })

  // The few most recent runs with insights.
  const recent = (runsQuery.data?.runs ?? []).filter((r) => r.insights_count > 0).slice(0, 5)
  const resultQueries = useQueries({
    queries: recent.map((run) => ({
      queryKey: [active?.id ?? 'none', 'learn', 'results', run.run_id],
      enabled: Boolean(conn),
      queryFn: ({ signal }: { signal: AbortSignal }) => runResults(conn!, run.run_id, { signal }),
    })),
  })

  const insights = useMemo<Insight[]>(() => {
    const seen = new Set<string>()
    const out: Insight[] = []
    for (const query of resultQueries) {
      const data = query.data as RunResults | undefined
      for (const raw of data?.insights ?? []) {
        const insight = asInsight(raw)
        if (insight && !seen.has(insight.title.toLowerCase())) {
          seen.add(insight.title.toLowerCase())
          out.push(insight)
        }
      }
    }
    return out
  }, [resultQueries])

  const endpointMissing = runsQuery.error instanceof ApiError && runsQuery.error.status === 404

  return (
    <div className="space-y-4">
      <PageHeader
        title="Learn"
        description="Recent agent-produced insights about your users."
        actions={
          <Button size="sm" variant="outline" asChild>
            <Link to="/analytics/funnels">
              <BarChart3 />
              Investigate
            </Link>
          </Button>
        }
      />

      {runsQuery.isPending ? <Skeleton className="h-40 w-full" /> : null}
      {runsQuery.error && !endpointMissing ? (
        <ErrorState error={runsQuery.error} onRetry={() => void runsQuery.refetch()} />
      ) : null}

      {runsQuery.data && insights.length === 0 ? (
        <EmptyState
          icon={<Lightbulb className="h-8 w-8" />}
          title="No insights yet"
          description="Run the loop's behavior analysis to surface what your users are doing."
        />
      ) : null}

      {insights.length > 0 ? (
        <div className="space-y-2">
          <SectionHeading title="Insights" count={insights.length} description="from recent runs" />
          {insights.map((insight, index) => {
            const evidence: Evidence[] = []
            if (insight.confidence) evidence.push({ label: 'confidence', value: insight.confidence })
            if (insight.impact) evidence.push({ label: 'impact', value: insight.impact })
            return (
              <Card key={`${insight.title}-${index}`}>
                <CardContent className="space-y-1.5 p-4">
                  <div className="flex items-start gap-2.5">
                    <Lightbulb className="mt-0.5 h-4 w-4 shrink-0 text-muted-foreground" />
                    <p className="min-w-0 flex-1 text-sm font-medium">{insight.title}</p>
                  </div>
                  {insight.description ? (
                    <p className="pl-7 text-sm text-muted-foreground">{insight.description}</p>
                  ) : null}
                  {evidence.length > 0 ? <EvidenceRow items={evidence} className="pl-7" /> : null}
                  {insight.recommended_action ? (
                    <p className="pl-7 text-xs text-muted-foreground">
                      Recommended: {insight.recommended_action}
                    </p>
                  ) : null}
                </CardContent>
              </Card>
            )
          })}
        </div>
      ) : null}

    </div>
  )
}
