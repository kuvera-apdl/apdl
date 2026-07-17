// Health probes never throw — degraded bodies (e.g. ingestion's 503) are data,
// not errors, so panels can render them.
import { normalizeBaseUrl, notifyUnauthorized } from './http'

// The analytics services that the health grid + integration verification cover.
// Distinct from the full ServiceName set (which also includes codegen — a
// separate concern not in the event→flag→query data path).
export type HealthServiceName = 'ingestion' | 'config' | 'query' | 'agents'

export interface ServiceDescriptor {
  service: HealthServiceName
  label: string
  hasReady: boolean
}

export interface ServiceHealthTarget {
  service: HealthServiceName
  baseUrl: string
}

export const CORE_SERVICE_DESCRIPTORS: ServiceDescriptor[] = [
  { service: 'ingestion', label: 'Ingestion', hasReady: false },
  { service: 'config', label: 'Config', hasReady: true },
  { service: 'query', label: 'Query', hasReady: true },
]

export const SERVICE_DESCRIPTORS: ServiceDescriptor[] = [
  ...CORE_SERVICE_DESCRIPTORS,
  { service: 'agents', label: 'Agents', hasReady: true },
]

export interface ProbeResult {
  ok: boolean
  status: number | null
  latencyMs: number
  body: unknown
  error: string | null
}

export async function probe(
  url: string,
  options: { timeoutMs?: number } = {},
): Promise<ProbeResult> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 5000)
  const startedAt = performance.now()
  try {
    const response = await fetch(url, { credentials: 'same-origin', signal: controller.signal })
    notifyUnauthorized(response)
    let body: unknown = null
    try {
      body = await response.json()
    } catch {
      body = null
    }
    return {
      ok: response.ok,
      status: response.status,
      latencyMs: performance.now() - startedAt,
      body,
      error: null,
    }
  } catch (error) {
    return {
      ok: false,
      status: null,
      latencyMs: performance.now() - startedAt,
      body: null,
      error: error instanceof Error ? error.message : 'request failed',
    }
  } finally {
    clearTimeout(timer)
  }
}

export interface ServiceHealth {
  service: ServiceHealthTarget['service']
  health: ProbeResult
  ready: ProbeResult | null
}

export async function checkService(target: ServiceHealthTarget): Promise<ServiceHealth> {
  const baseUrl = normalizeBaseUrl(target.baseUrl)
  const descriptor = SERVICE_DESCRIPTORS.find((entry) => entry.service === target.service)
  const [health, ready] = await Promise.all([
    probe(`${baseUrl}/health`),
    descriptor?.hasReady
      ? probe(`${baseUrl}/ready`)
      : Promise.resolve(null),
  ])
  return { service: target.service, health, ready }
}

export type HealthLevel = 'ok' | 'degraded' | 'unreachable'

export function healthLevel(result: ServiceHealth): HealthLevel {
  if (result.health.status === null) return 'unreachable'
  if (!result.health.ok) return 'degraded'
  const body = result.health.body as { status?: unknown } | null
  if (typeof body?.status === 'string' && body.status !== 'ok') return 'degraded'
  if (result.ready) {
    const readyBody = result.ready.body as { status?: unknown } | null
    if (!result.ready.ok || readyBody?.status !== 'ready') return 'degraded'
  }
  return 'ok'
}
