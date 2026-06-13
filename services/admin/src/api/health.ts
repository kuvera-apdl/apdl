// Health probes never throw — degraded bodies (e.g. ingestion's 503) are data,
// not errors, so panels can render them.
import { normalizeBaseUrl } from './http'

export interface ServiceDescriptor {
  service: ServiceHealthTarget['service']
  label: string
  hasReady: boolean
}

export interface ServiceHealthTarget {
  service: 'ingestion' | 'config' | 'query' | 'agents'
  baseUrl: string
  apiKey?: string
}

export const SERVICE_DESCRIPTORS: ServiceDescriptor[] = [
  { service: 'ingestion', label: 'Ingestion', hasReady: false },
  { service: 'config', label: 'Config', hasReady: false },
  { service: 'query', label: 'Query', hasReady: true },
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
  options: { apiKey?: string; timeoutMs?: number } = {},
): Promise<ProbeResult> {
  const controller = new AbortController()
  const timer = setTimeout(() => controller.abort(), options.timeoutMs ?? 5000)
  const startedAt = performance.now()
  try {
    const headers: Record<string, string> = {}
    if (options.apiKey) headers['X-API-Key'] = options.apiKey
    const response = await fetch(url, { headers, signal: controller.signal })
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
    probe(`${baseUrl}/health`, { apiKey: target.apiKey }),
    descriptor?.hasReady
      ? probe(`${baseUrl}/ready`, { apiKey: target.apiKey })
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
