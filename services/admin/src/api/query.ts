// Query-service client (plan §5.5). All requests are POST bodies with
// project_id; outgoing payloads are parsed against the canonical mirrors.
import { apiCurl, request, type ServiceConnection } from './http'
import {
  breakdownRequestSchema,
  breakdownResponseSchema,
  cohortRequestSchema,
  cohortResponseSchema,
  eventCountRequestSchema,
  eventCountResponseSchema,
  funnelRequestSchema,
  funnelResponseSchema,
  retentionRequestSchema,
  retentionResponseSchema,
  timeseriesRequestSchema,
  timeseriesResponseSchema,
} from './schemas/query'
import type {
  BreakdownRequest,
  BreakdownResponse,
  CohortRequest,
  CohortResponse,
  EventCountRequest,
  EventCountResponse,
  FunnelRequest,
  FunnelResponse,
  RetentionRequest,
  RetentionResponse,
  TimeseriesRequest,
  TimeseriesResponse,
} from './types/query'
import type { CurlSpec } from '@/lib/curl'

export const QUERY_PATHS = {
  count: '/v1/query/events/count',
  timeseries: '/v1/query/events/timeseries',
  breakdown: '/v1/query/events/breakdown',
  funnel: '/v1/query/funnel',
  retention: '/v1/query/retention',
  cohort: '/v1/query/cohort',
} as const

export function countEvents(conn: ServiceConnection, body: EventCountRequest): Promise<EventCountResponse> {
  return request(conn, QUERY_PATHS.count, {
    method: 'POST',
    body: eventCountRequestSchema.parse(body),
    schema: eventCountResponseSchema,
  })
}

export function timeseriesEvents(
  conn: ServiceConnection,
  body: TimeseriesRequest,
): Promise<TimeseriesResponse> {
  return request(conn, QUERY_PATHS.timeseries, {
    method: 'POST',
    body: timeseriesRequestSchema.parse(body),
    schema: timeseriesResponseSchema,
  })
}

export function breakdownEvents(
  conn: ServiceConnection,
  body: BreakdownRequest,
): Promise<BreakdownResponse> {
  return request(conn, QUERY_PATHS.breakdown, {
    method: 'POST',
    body: breakdownRequestSchema.parse(body),
    schema: breakdownResponseSchema,
  })
}

export function runFunnel(conn: ServiceConnection, body: FunnelRequest): Promise<FunnelResponse> {
  return request(conn, QUERY_PATHS.funnel, {
    method: 'POST',
    body: funnelRequestSchema.parse(body),
    schema: funnelResponseSchema,
  })
}

export function runRetention(
  conn: ServiceConnection,
  body: RetentionRequest,
): Promise<RetentionResponse> {
  return request(conn, QUERY_PATHS.retention, {
    method: 'POST',
    body: retentionRequestSchema.parse(body),
    schema: retentionResponseSchema,
  })
}

export function runCohort(conn: ServiceConnection, body: CohortRequest): Promise<CohortResponse> {
  return request(conn, QUERY_PATHS.cohort, {
    method: 'POST',
    body: cohortRequestSchema.parse(body),
    schema: cohortResponseSchema,
  })
}

export function queryCurl(conn: ServiceConnection, path: string, body: unknown): CurlSpec {
  return apiCurl(conn, 'POST', path, { body })
}
