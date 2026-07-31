import { render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { describe, expect, test } from 'vitest'

import type { ProbeResult, ServiceHealth } from '../../src/api/health'
import { ServiceHealthCard } from '../../src/features/system/ServiceHealthCard'

function probe(overrides: Partial<ProbeResult> = {}): ProbeResult {
  return {
    ok: true,
    status: 200,
    latencyMs: 4,
    body: { status: 'ok' },
    error: null,
    ...overrides,
  }
}

function configResult(ready: ProbeResult | null): ServiceHealth {
  return {
    service: 'config',
    health: probe({
      body: {
        status: 'ok',
        service: 'apdl-config',
        postgres: 'legacy-health-value',
        redis: 'legacy-health-value',
        sse_connections: 999,
      },
    }),
    ready,
  }
}

function renderCard(result: ServiceHealth, detailed = false) {
  return render(
    <MemoryRouter>
      <ServiceHealthCard
        label={result.service === 'config' ? 'Config' : result.service}
        result={result}
        isLoading={false}
        detailed={detailed}
      />
    </MemoryRouter>,
  )
}

describe('ServiceHealthCard Config readiness', () => {
  test.each([0, 7])('reads healthy dependencies and %i SSE connections from readiness', (count) => {
    renderCard(
      configResult(
        probe({
          body: {
            status: 'ready',
            checks: { postgres: 'ready', redis: 'ready' },
            sse: { active_connections: count },
          },
        }),
      ),
    )

    expect(screen.getByText(`pg: ready · redis: ready · sse: ${count}`)).toBeVisible()
    expect(screen.queryByText(/legacy-health-value/)).not.toBeInTheDocument()
    expect(screen.queryByText(/999/)).not.toBeInTheDocument()
  })

  test('shows degraded dependency states consistently with readiness', () => {
    renderCard(
      configResult(
        probe({
          ok: false,
          status: 503,
          body: {
            status: 'not_ready',
            checks: { postgres: 'not_ready', redis: 'ready' },
            sse: { active_connections: 2 },
          },
        }),
      ),
    )

    expect(screen.getByText('degraded')).toBeVisible()
    expect(screen.getByText('pg: not_ready · redis: ready · sse: 2')).toBeVisible()
  })

  test('handles missing, failed, and malformed readiness without undefined values', () => {
    const { rerender } = renderCard(configResult(null))
    expect(screen.getByText('readiness: unknown')).toBeVisible()

    rerender(
      <MemoryRouter>
        <ServiceHealthCard
          label="Config"
          result={configResult(
            probe({ ok: false, status: null, body: null, error: 'request timed out' }),
          )}
          isLoading={false}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('readiness error: request timed out')).toBeVisible()

    rerender(
      <MemoryRouter>
        <ServiceHealthCard
          label="Config"
          result={configResult(
            probe({
              body: {
                status: 'ready',
                checks: { postgres: 1 },
                sse: { active_connections: 'many' },
              },
            }),
          )}
          isLoading={false}
        />
      </MemoryRouter>,
    )
    expect(screen.getByText('pg: unknown · redis: unknown · sse: unknown')).toBeVisible()
    expect(screen.queryByText(/undefined/)).not.toBeInTheDocument()
  })
})

describe('ServiceHealthCard detailed probes', () => {
  test.each(['config', 'query', 'agents'] as const)(
    'shows separate liveness and readiness payloads for %s',
    (service) => {
      renderCard(
        {
          service,
          health: probe({ latencyMs: 3, body: { status: 'ok', service: `apdl-${service}` } }),
          ready: probe({ latencyMs: 8, body: { status: 'ready', marker: `${service}-ready` } }),
        },
        true,
      )

      const health = screen.getByRole('region', { name: '/health response' })
      const ready = screen.getByRole('region', { name: '/ready response' })
      expect(within(health).getByText('/health')).toHaveTextContent('HTTP 200')
      expect(health).toHaveTextContent(`apdl-${service}`)
      expect(within(ready).getByText('/ready')).toHaveTextContent('HTTP 200')
      expect(ready).toHaveTextContent(`${service}-ready`)
    },
  )

  test('keeps Ingestion liveness-only', () => {
    renderCard(
      {
        service: 'ingestion',
        health: probe({ body: { status: 'ok', service: 'apdl-ingestion' } }),
        ready: null,
      },
      true,
    )

    expect(screen.getByRole('region', { name: '/health response' })).toBeVisible()
    expect(screen.queryByRole('region', { name: '/ready response' })).not.toBeInTheDocument()
  })
})
