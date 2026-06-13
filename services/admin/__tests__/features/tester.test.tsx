import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, test } from 'vitest'

import { TooltipProvider } from '../../src/components/ui/tooltip'
import { WorkspaceProvider } from '../../src/core/workspace'
import { TesterTab } from '../../src/features/flags/tabs/TesterTab'
import { makeFlag, seedWorkspace } from '../helpers/fixtures'

beforeEach(() => {
  localStorage.clear()
  seedWorkspace()
})

// Deterministic fixture: one rule (plan equals pro) at 100% rollout, 100%
// fallthrough — every evaluation lands somewhere predictable.
const flag = makeFlag({
  rules: [
    {
      id: 'rule_pro',
      name: 'pro users',
      conditions: [{ attribute: 'plan', operator: 'equals', value: 'pro' }],
      rollout: { percentage: 100, bucket_by: 'user_id' },
    },
  ],
  fallthrough: { rollout: { percentage: 100, bucket_by: 'user_id' } },
})

function renderTester() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } })
  render(
    <WorkspaceProvider>
      <QueryClientProvider client={queryClient}>
        <TooltipProvider>
          <TesterTab flag={flag} />
        </TooltipProvider>
      </QueryClientProvider>
    </WorkspaceProvider>,
  )
}

describe('TesterTab', () => {
  test('evaluates the default context to fallthrough with the rule annotated as failed', async () => {
    renderTester()
    expect(await screen.findByText('fallthrough')).toBeInTheDocument()
    expect(screen.getByText('conditions failed')).toBeInTheDocument()
    expect(screen.getByText(/config v3/)).toBeInTheDocument()
  })

  test('adding the matching attribute flips the result to rule_match', async () => {
    renderTester()
    await userEvent.click(screen.getByRole('button', { name: 'Add attribute' }))
    await userEvent.type(screen.getByLabelText('Attribute 1 key'), 'plan')
    await userEvent.type(screen.getByLabelText('Attribute 1 value'), 'pro')

    expect(await screen.findByText('rule_match')).toBeInTheDocument()
    expect(screen.getByText('matched')).toBeInTheDocument()
    // The why-text explains the decision path.
    expect(screen.getByText(/Matched rule "pro users"/)).toBeInTheDocument()
  })

  test('hides server verification for client-mode flags and explains why', async () => {
    renderTester()
    expect(screen.queryByRole('button', { name: 'Verify on server' })).not.toBeInTheDocument()
    expect(
      await screen.findByText(/Server verification is unavailable for client-mode flags/),
    ).toBeInTheDocument()
  })

  test('runs the population simulator with the shared attributes', async () => {
    renderTester()
    expect(await screen.findByText(/Entry path — 10,000 simulated users/)).toBeInTheDocument()
    expect(screen.getByText(/Variant split among/)).toBeInTheDocument()
  })
})
