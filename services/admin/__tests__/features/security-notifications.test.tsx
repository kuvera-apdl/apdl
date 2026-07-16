import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { setupServer } from 'msw/node'
import { afterAll, afterEach, beforeAll, expect, test } from 'vitest'

import { SecurityNotificationBanner } from '../../src/components/layout/SecurityNotificationBanner'

const notification = {
  notification_id: '70000000-0000-4000-8000-000000000007',
  kind: 'suspicious_login_activity',
  status: 'unread',
  observed_failures: 50,
  window_started_at: '2026-07-16T14:00:00Z',
  last_detected_at: '2026-07-16T15:00:00Z',
  created_at: '2026-07-16T15:00:00Z',
}

const server = setupServer()

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }))
afterEach(() => server.resetHandlers())
afterAll(() => server.close())

test('shows and acknowledges a durable suspicious-login notification', async () => {
  let unread = true
  let acknowledged = false
  server.use(
    http.get('*/api/auth/security-notifications', () =>
      HttpResponse.json(unread ? [notification] : []),
    ),
    http.post(
      `*/api/auth/security-notifications/${notification.notification_id}/acknowledge`,
      () => {
        acknowledged = true
        unread = false
        return new HttpResponse(null, { status: 204 })
      },
    ),
  )
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  })
  render(
    <QueryClientProvider client={queryClient}>
      <SecurityNotificationBanner />
    </QueryClientProvider>,
  )

  expect(await screen.findByText('Suspicious sign-in activity detected')).toBeInTheDocument()
  expect(screen.getByText(/50 failed attempts/)).toBeInTheDocument()
  await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }))

  await waitFor(() => expect(acknowledged).toBe(true))
  await waitFor(() =>
    expect(screen.queryByText('Suspicious sign-in activity detected')).not.toBeInTheDocument(),
  )
})
