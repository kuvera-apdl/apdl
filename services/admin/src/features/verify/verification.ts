// Integration verification engine (plan §5.7): prove the pipeline end-to-end
// — health → ingest a labeled test event → poll until ClickHouse answers →
// flag bootstrap round-trip (X-Cache behavior) → live SSE. Mirrors
// scripts/dev.sh smoke, including its exact-one transport assertion.
import { checkService, CORE_SERVICE_DESCRIPTORS, healthLevel } from '@/api/health'
import { notifyUnauthorized, request } from '@/api/http'
import { countEvents } from '@/api/query'
import { flagCollectionSchema } from '@/api/schemas/flags'
import type { StreamStatus } from '@/api/sse'
import { serviceBaseUrl, serviceConnection, type Workspace } from '@/core/workspace'

export const VERIFICATION_EVENT_NAME = 'apdl_console_verification'

export type StepStatus = 'idle' | 'running' | 'ok' | 'fail'

export interface StepState {
  id: StepId
  label: string
  status: StepStatus
  detail: string
  hint?: string
  durationMs?: number
  data?: unknown
}

export type StepId = 'health' | 'ingest' | 'pipeline' | 'flags' | 'sse'

export const VERIFY_STEP_DEFS: { id: StepId; label: string }[] = [
  { id: 'health', label: 'All three core services healthy' },
  { id: 'ingest', label: 'Send a labeled test event' },
  { id: 'pipeline', label: 'Event arrives in ClickHouse (writer flush ≤ 5s)' },
  { id: 'flags', label: 'Flag bootstrap round-trip (X-Cache)' },
  { id: 'sse', label: 'Live SSE stream delivering' },
]

export interface LiveSnapshot {
  status: StreamStatus
  lastEventAt: number | null
  hasServedFlags: boolean
}

export interface VerificationOptions {
  workspace: Workspace
  projectId: string
  getLive: () => LiveSnapshot
  update: (id: StepId, patch: Partial<StepState>) => void
  /** Injectable for tests. */
  sleep?: (ms: number) => Promise<void>
  pollAttempts?: number
  pollIntervalMs?: number
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

async function timed<T>(work: () => Promise<T>): Promise<{ result: T; durationMs: number }> {
  const startedAt = performance.now()
  const result = await work()
  return { result, durationMs: performance.now() - startedAt }
}

export async function runVerification(options: VerificationOptions): Promise<boolean> {
  const {
    workspace,
    projectId,
    getLive,
    update,
    sleep = defaultSleep,
    pollAttempts = 30,
    pollIntervalMs = 2000,
  } = options

  // Step 1 — health.
  update('health', { status: 'running', detail: 'Probing service liveness and readiness…' })
  const { result: healthResults, durationMs: healthMs } = await timed(() =>
    Promise.all(
      CORE_SERVICE_DESCRIPTORS.map(({ service }) =>
        checkService({
          service,
          baseUrl: serviceBaseUrl(workspace, service),
        }),
      ),
    ),
  )
  const unhealthy = healthResults.filter((result) => healthLevel(result) !== 'ok')
  if (unhealthy.length > 0) {
    update('health', {
      status: 'fail',
      durationMs: healthMs,
      detail: `Unhealthy: ${unhealthy.map((result) => result.service).join(', ')}`,
      hint: 'Start the stack with scripts/dev.sh up-core, or check make status.',
      data: healthResults,
    })
    return false
  }
  update('health', {
    status: 'ok',
    durationMs: healthMs,
    detail: '3/3 core services healthy',
    data: healthResults,
  })

  // Step 2 — send a labeled test event.
  const verificationId = crypto.randomUUID()
  const ingestionConn = serviceConnection(workspace, 'ingestion')
  const testEvent = {
    event: VERIFICATION_EVENT_NAME,
    type: 'track',
    user_id: 'apdl-console-verifier',
    timestamp: new Date().toISOString(),
    properties: { verification_id: verificationId },
    context: { library: { name: 'apdl-admin', version: '1' } },
    message_id: `admin_verification_${verificationId}`,
  }
  const sendEvent = () =>
    request<{ accepted: number; failed?: number }>(ingestionConn, '/v1/events', {
      method: 'POST',
      body: { events: [testEvent] },
    })

  update('ingest', { status: 'running', detail: 'POST /v1/events…' })
  try {
    const { result: ingestResult, durationMs } = await timed(sendEvent)
    if (ingestResult.accepted !== 1) {
      update('ingest', {
        status: 'fail',
        durationMs,
        detail: `Ingestion accepted ${ingestResult.accepted} events`,
        data: ingestResult,
      })
      return false
    }
    update('ingest', {
      status: 'ok',
      durationMs,
      detail: `202 accepted (verification_id ${verificationId.slice(0, 8)}…)`,
      data: ingestResult,
    })
  } catch (error) {
    update('ingest', {
      status: 'fail',
      detail: error instanceof Error ? error.message : 'Ingestion request failed',
      hint: 'If this is a 429, wait for the per-project rate limit to refill and re-run.',
    })
    return false
  }

  // Step 3 — poll the query service until the pipeline lands the event.
  update('pipeline', { status: 'running', detail: 'Polling /v1/query/events/count…' })
  const queryConn = serviceConnection(workspace, 'query')
  // verification_id is globally unique, so the date range is only a coarse
  // pre-filter — bracket it wide in UTC so no timezone/clock skew between the
  // browser and the event store can hide a just-ingested event. (The count
  // endpoint requires both dates.)
  const utcDate = (offsetDays: number) =>
    new Date(Date.now() + offsetDays * 86_400_000).toISOString().slice(0, 10)
  const pollBody = {
    project_id: projectId,
    start_date: utcDate(-2),
    end_date: utcDate(1),
    selectors: [
      {
        event_name: VERIFICATION_EVENT_NAME,
        filters: [{ property: 'verification_id', operator: 'eq' as const, value: verificationId }],
      },
    ],
  }
  const pollStartedAt = performance.now()
  let found = false
  for (let attempt = 1; attempt <= pollAttempts; attempt++) {
    try {
      const counts = await countEvents(queryConn, pollBody)
      if (counts.total_events > 1) {
        update('pipeline', {
          status: 'fail',
          durationMs: performance.now() - pollStartedAt,
          detail: `Expected exactly one event; Query returned ${counts.total_events}`,
          data: counts,
        })
        return false
      }
      if (counts.total_events === 1) {
        const result = counts.results[0]
        const expectedSelector = `${VERIFICATION_EVENT_NAME}[verification_id eq ${verificationId}]`
        if (
          counts.total_users !== 1 ||
          counts.results.length !== 1 ||
          result?.selector !== expectedSelector ||
          result?.event_name !== VERIFICATION_EVENT_NAME ||
          result.event_count !== 1 ||
          result.unique_users !== 1
        ) {
          update('pipeline', {
            status: 'fail',
            durationMs: performance.now() - pollStartedAt,
            detail: 'Query returned a non-canonical exact-count projection',
            data: counts,
          })
          return false
        }
        update('pipeline', {
          status: 'ok',
          durationMs: performance.now() - pollStartedAt,
          detail: `Arrived after ${attempt} attempt${attempt === 1 ? '' : 's'} (${((performance.now() - pollStartedAt) / 1000).toFixed(1)}s ingest→query)`,
          data: counts,
        })
        found = true
        break
      }
    } catch {
      // transient query errors during polling are tolerated
    }
    update('pipeline', { detail: `Attempt ${attempt}/${pollAttempts}…` })
    await sleep(pollIntervalMs)
  }
  if (!found) {
    update('pipeline', {
      status: 'fail',
      durationMs: performance.now() - pollStartedAt,
      detail: `Event not visible after ${Math.round((pollAttempts * pollIntervalMs) / 1000)}s`,
      hint: 'Is the clickhouse-writer running? scripts/dev.sh logs clickhouse-writer',
    })
    return false
  }

  // Step 4 — flag bootstrap round-trip with X-Cache observation.
  update('flags', { status: 'running', detail: 'GET /v1/flags ×2…' })
  try {
    const url = `${serviceBaseUrl(workspace, 'config')}/v1/flags`
    const fetchOnce = async () => {
      const response = await fetch(url, { credentials: 'same-origin' })
      notifyUnauthorized(response)
      const cache = response.headers.get('x-cache') ?? 'n/a'
      const body: unknown = await response.json()
      return { ok: response.ok, cache, body }
    }
    const { result, durationMs } = await timed(async () => {
      const first = await fetchOnce()
      const second = await fetchOnce()
      return { first, second }
    })
    const parsed = flagCollectionSchema.safeParse(result.second.body)
    if (!result.first.ok || !result.second.ok || !parsed.success) {
      update('flags', {
        status: 'fail',
        durationMs,
        detail: !parsed.success
          ? 'Bootstrap payload does not match the canonical schema'
          : 'GET /v1/flags failed',
        data: result,
      })
      return false
    }
    update('flags', {
      status: 'ok',
      durationMs,
      detail: `schema_version 2 · ${parsed.data.flags.length} client-visible flags · X-Cache ${result.first.cache} → ${result.second.cache}`,
      data: result,
    })
  } catch (error) {
    update('flags', {
      status: 'fail',
      detail: error instanceof Error ? error.message : 'Flag round-trip failed',
    })
    return false
  }

  // Step 5 — the console's own SSE stream.
  const live = getLive()
  const heartbeatFresh = live.lastEventAt !== null && Date.now() - live.lastEventAt < 40_000
  if (live.status === 'open' && heartbeatFresh && live.hasServedFlags) {
    update('sse', {
      status: 'ok',
      detail: 'Stream open, config snapshot received, heartbeat within 40s',
    })
  } else {
    update('sse', {
      status: 'fail',
      detail: `Stream ${live.status}${live.hasServedFlags ? '' : ', no config snapshot yet'}`,
      hint: 'The server heartbeats every 15s — check the config service and your project access.',
    })
    return false
  }

  return true
}
