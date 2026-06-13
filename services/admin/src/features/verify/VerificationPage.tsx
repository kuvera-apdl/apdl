// /settings/verify (plan §5.7): the console-native equivalent of
// `scripts/dev.sh smoke` — a copy-paste check for SDK installers.
import { CheckCircle2, Circle, Loader2, Play, XCircle } from 'lucide-react'
import { useRef, useState } from 'react'

import { JsonView } from '@/components/shared/JsonView'
import { PageHeader } from '@/components/shared/PageHeader'
import { Button } from '@/components/ui/button'
import { Card, CardContent } from '@/components/ui/card'
import { useLive } from '@/core/live'
import { useWorkspace } from '@/core/workspace'
import { formatMs } from '@/lib/format'
import { cn } from '@/lib/utils'

import {
  runVerification,
  VERIFY_STEP_DEFS,
  type StepId,
  type StepState,
} from './verification'

function initialSteps(): StepState[] {
  return VERIFY_STEP_DEFS.map((def) => ({ ...def, status: 'idle', detail: '' }))
}

function StepIcon({ status }: { status: StepState['status'] }) {
  if (status === 'ok') return <CheckCircle2 className="h-5 w-5 text-emerald-600" />
  if (status === 'fail') return <XCircle className="h-5 w-5 text-destructive" />
  if (status === 'running') return <Loader2 className="h-5 w-5 animate-spin text-muted-foreground" />
  return <Circle className="h-5 w-5 text-muted-foreground/40" />
}

export function VerificationPage() {
  const { active, projectId } = useWorkspace()
  const { state: liveState, servedFlags } = useLive()
  const [steps, setSteps] = useState<StepState[]>(initialSteps)
  const [running, setRunning] = useState(false)
  const [verdict, setVerdict] = useState<'ok' | 'fail' | null>(null)
  const [totalMs, setTotalMs] = useState<number | null>(null)
  // The engine reads live state at step 5 — keep the latest values in a ref.
  const liveRef = useRef({ liveState, servedFlags })
  liveRef.current = { liveState, servedFlags }

  const update = (id: StepId, patch: Partial<StepState>) => {
    setSteps((previous) => previous.map((step) => (step.id === id ? { ...step, ...patch } : step)))
  }

  const run = async () => {
    if (!active || !projectId) return
    setSteps(initialSteps())
    setVerdict(null)
    setTotalMs(null)
    setRunning(true)
    const startedAt = performance.now()
    try {
      const passed = await runVerification({
        workspace: active,
        projectId,
        update,
        getLive: () => ({
          status: liveRef.current.liveState.status,
          lastEventAt: liveRef.current.liveState.lastEventAt,
          hasServedFlags: liveRef.current.servedFlags !== null,
        }),
      })
      setVerdict(passed ? 'ok' : 'fail')
    } finally {
      setTotalMs(performance.now() - startedAt)
      setRunning(false)
    }
  }

  return (
    <div className="max-w-3xl space-y-5">
      <PageHeader
        title="Verify integration"
        description="Prove the Loop end-to-end: events ingest, the pipeline writes, queries answer, flags serve, and the stream lives."
        actions={
          <Button onClick={() => void run()} disabled={running || !active}>
            {running ? <Loader2 className="animate-spin" /> : <Play />}
            Run verification
          </Button>
        }
      />

      {verdict === 'ok' ? (
        <div className="rounded-lg border border-emerald-300 bg-emerald-50 p-4 text-sm dark:border-emerald-900 dark:bg-emerald-950/30">
          <span className="font-semibold">Loop verified</span> — events ingest, pipeline writes,
          queries answer, flags serve, stream lives.
          {totalMs !== null ? ` Total ${formatMs(totalMs)}.` : ''}
        </div>
      ) : null}
      {verdict === 'fail' ? (
        <div className="rounded-lg border border-destructive/60 bg-destructive/10 p-4 text-sm">
          <span className="font-semibold">Verification failed</span> — see the failing step below.
          {totalMs !== null ? ` Total ${formatMs(totalMs)}.` : ''}
        </div>
      ) : null}

      <Card>
        <CardContent className="divide-y p-0">
          {steps.map((step, index) => (
            <div key={step.id} className={cn('flex gap-3 p-4', step.status === 'idle' && 'opacity-60')}>
              <StepIcon status={step.status} />
              <div className="min-w-0 flex-1 space-y-1">
                <div className="flex items-baseline justify-between gap-2">
                  <p className="text-sm font-medium">
                    {index + 1}. {step.label}
                  </p>
                  {step.durationMs !== undefined ? (
                    <span className="shrink-0 text-xs tabular-nums text-muted-foreground">
                      {formatMs(step.durationMs)}
                    </span>
                  ) : null}
                </div>
                {step.detail ? <p className="text-xs text-muted-foreground">{step.detail}</p> : null}
                {step.hint && step.status === 'fail' ? (
                  <p className="text-xs font-medium text-amber-700 dark:text-amber-400">{step.hint}</p>
                ) : null}
                {step.data !== undefined ? (
                  <details>
                    <summary className="cursor-pointer text-xs text-muted-foreground hover:text-foreground">
                      Response
                    </summary>
                    <JsonView data={step.data} className="mt-1 max-h-48" />
                  </details>
                ) : null}
              </div>
            </div>
          ))}
        </CardContent>
      </Card>

      <p className="text-xs text-muted-foreground">
        The test event is named <code className="font-mono">apdl_console_verification</code> so it
        can be filtered out of real analytics. The pipeline step covers the writer's 5s flush and
        first consumer-group creation, re-sending once at attempt 5 exactly like{' '}
        <code className="font-mono">dev.sh smoke</code>.
      </p>
    </div>
  )
}
