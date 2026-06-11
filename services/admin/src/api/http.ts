// Single fetch wrapper used by every service client: canonical headers, error
// envelope normalization, and retry for idempotent GETs only (409/422 are
// semantic, never transient).
import type { ZodType } from 'zod'

export interface ServiceConnection {
  baseUrl: string
  apiKey: string
  actor: string
}

export type HttpMethod = 'GET' | 'POST' | 'PUT' | 'DELETE'

export type QueryParams = Record<string, string | number | boolean | undefined>

export interface RequestOptions<T> {
  method?: HttpMethod
  query?: QueryParams
  body?: unknown
  signal?: AbortSignal
  /** Canonical response mirror; a mismatch throws ApiError(code: "schema_mismatch"). */
  schema?: ZodType<T>
}

export class ApiError extends Error {
  readonly status: number
  readonly code: string
  readonly body: unknown

  constructor(status: number, code: string, message: string, body: unknown = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
    this.body = body
  }
}

const GET_RETRIES = 2

export function normalizeBaseUrl(url: string): string {
  return url.replace(/\/+$/, '')
}

export function buildUrl(baseUrl: string, path: string, query?: QueryParams): string {
  const url = `${normalizeBaseUrl(baseUrl)}${path}`
  if (!query) return url
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(query)) {
    if (value !== undefined) params.set(key, String(value))
  }
  const qs = params.toString()
  return qs ? `${url}?${qs}` : url
}

interface ErrorEnvelope {
  error?: unknown
  message?: unknown
  detail?: unknown
}

export function errorFromResponse(status: number, body: unknown, statusText: string): ApiError {
  const envelope: ErrorEnvelope = typeof body === 'object' && body !== null ? body : {}
  if (typeof envelope.error === 'string' && typeof envelope.message === 'string') {
    return new ApiError(status, envelope.error, envelope.message, body)
  }
  if (Array.isArray(envelope.detail)) {
    // FastAPI request validation: detail is a list of {loc, msg, type}.
    const first = envelope.detail[0] as { loc?: unknown[]; msg?: unknown } | undefined
    const loc = Array.isArray(first?.loc) ? first.loc.slice(1).join('.') : ''
    const msg = typeof first?.msg === 'string' ? first.msg : 'Invalid request'
    return new ApiError(status, 'validation_error', loc ? `${loc}: ${msg}` : msg, body)
  }
  if (typeof envelope.detail === 'string') {
    return new ApiError(status, `http_${status}`, envelope.detail, body)
  }
  return new ApiError(status, `http_${status}`, statusText || `Request failed with status ${status}`, body)
}

function retryDelayMs(retry: number): number {
  return 250 * 2 ** retry + Math.random() * 100
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

export async function request<T>(
  conn: ServiceConnection,
  path: string,
  options: RequestOptions<T> = {},
): Promise<T> {
  const method = options.method ?? 'GET'
  const url = buildUrl(conn.baseUrl, path, options.query)

  const headers: Record<string, string> = {}
  if (conn.apiKey) headers['X-API-Key'] = conn.apiKey
  if (method !== 'GET' && conn.actor) headers['x-apdl-actor'] = conn.actor
  if (options.body !== undefined) headers['Content-Type'] = 'application/json'

  const maxAttempts = method === 'GET' ? GET_RETRIES + 1 : 1
  let lastError: unknown = null

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (attempt > 0) await sleep(retryDelayMs(attempt - 1))

    let response: Response
    try {
      response = await fetch(url, {
        method,
        headers,
        body: options.body !== undefined ? JSON.stringify(options.body) : undefined,
        signal: options.signal ?? null,
      })
    } catch (error) {
      if (options.signal?.aborted) throw error
      lastError = error
      continue
    }

    const text = await response.text()
    let data: unknown = null
    if (text) {
      try {
        data = JSON.parse(text)
      } catch {
        data = text
      }
    }

    if (!response.ok) {
      const apiError = errorFromResponse(response.status, data, response.statusText)
      if (response.status >= 500 && attempt < maxAttempts - 1) {
        lastError = apiError
        continue
      }
      throw apiError
    }

    if (!options.schema) return data as T
    const parsed = options.schema.safeParse(data)
    if (!parsed.success) {
      const issue = parsed.error.issues[0]
      const where = issue && issue.path.length > 0 ? ` at ${issue.path.join('.')}` : ''
      throw new ApiError(
        response.status,
        'schema_mismatch',
        `Response from ${path} does not match the canonical schema${where}: ${issue?.message ?? 'unknown issue'}`,
        data,
      )
    }
    return parsed.data
  }

  if (lastError instanceof ApiError) throw lastError
  throw new ApiError(
    0,
    'network_error',
    lastError instanceof Error ? lastError.message : 'Network request failed',
    null,
  )
}
