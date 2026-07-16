import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { beforeEach, describe, expect, test } from 'vitest'
import { z } from 'zod'

import { WorkspaceProvider, useWorkspace } from '../../src/core/workspace'
import { SavedViews } from '../../src/features/analytics/SavedViews'
import { makeWorkspace } from '../helpers/fixtures'

const viewSchema = z
  .object({
    query: z.string(),
  })
  .strict()

const workspaces = [
  makeWorkspace(),
  makeWorkspace({ id: 'alpha', name: 'alpha', projectId: 'alpha' }),
]

function SavedViewsHarness() {
  const { active, setActive } = useWorkspace()
  const [viewScreen, setViewScreen] = useState('events')
  const [loadedQuery, setLoadedQuery] = useState('')

  return (
    <>
      <div data-testid="active-workspace">{active?.id}</div>
      <button type="button" onClick={() => setActive('alpha')}>
        Switch workspace
      </button>
      <button
        type="button"
        onClick={() =>
          setViewScreen((currentScreen) =>
            currentScreen === 'events' ? 'funnels' : 'events',
          )
        }
      >
        Switch screen
      </button>
      <SavedViews
        screen={viewScreen}
        current={{ query: `current-${active?.id}-${viewScreen}` }}
        viewSchema={viewSchema}
        onLoad={(view) => setLoadedQuery(view.query)}
      />
      <output aria-label="Loaded query">{loadedQuery}</output>
    </>
  )
}

function renderSavedViews() {
  return render(
    <WorkspaceProvider initialWorkspaces={workspaces}>
      <SavedViewsHarness />
    </WorkspaceProvider>,
  )
}

beforeEach(() => {
  localStorage.clear()
  localStorage.setItem('apdl-admin:active-project', 'demo')
})

describe('SavedViews storage scope', () => {
  test('reloads workspace state before displaying or persisting views', async () => {
    const user = userEvent.setup()
    const demoViews = [{ name: 'Demo view', view: { query: 'demo-query' } }]
    const alphaViews = [{ name: 'Alpha view', view: { query: 'alpha-query' } }]
    localStorage.setItem('apdl-admin:views:demo:events', JSON.stringify(demoViews))
    localStorage.setItem('apdl-admin:views:alpha:events', JSON.stringify(alphaViews))

    renderSavedViews()

    await user.click(screen.getByRole('button', { name: 'Views' }))
    expect(await screen.findByText('Demo view')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Demo view')).not.toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Switch workspace' }))
    expect(screen.getByTestId('active-workspace')).toHaveTextContent('alpha')

    await user.click(screen.getByRole('button', { name: 'Views' }))
    expect(await screen.findByText('Alpha view')).toBeInTheDocument()
    expect(screen.queryByText('Demo view')).not.toBeInTheDocument()
    await user.click(screen.getByText('Alpha view'))
    expect(screen.getByRole('status', { name: 'Loaded query' })).toHaveTextContent('alpha-query')

    await user.click(screen.getByRole('button', { name: 'Views' }))
    await user.click(screen.getByText('Save current…'))
    await user.type(screen.getByRole('textbox', { name: 'View name' }), 'Fresh alpha')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(JSON.parse(localStorage.getItem('apdl-admin:views:alpha:events') ?? 'null')).toEqual([
      ...alphaViews,
      { name: 'Fresh alpha', view: { query: 'current-alpha-events' } },
    ])
    expect(JSON.parse(localStorage.getItem('apdl-admin:views:demo:events') ?? 'null')).toEqual(
      demoViews,
    )
  })

  test('reloads state when the screen changes without unmounting SavedViews', async () => {
    const user = userEvent.setup()
    const eventViews = [{ name: 'Event view', view: { query: 'events-query' } }]
    const funnelViews = [{ name: 'Funnel view', view: { query: 'funnels-query' } }]
    localStorage.setItem('apdl-admin:views:demo:events', JSON.stringify(eventViews))
    localStorage.setItem('apdl-admin:views:demo:funnels', JSON.stringify(funnelViews))

    renderSavedViews()

    await user.click(screen.getByRole('button', { name: 'Views' }))
    expect(await screen.findByText('Event view')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    await waitFor(() => expect(screen.queryByText('Event view')).not.toBeInTheDocument())

    await user.click(screen.getByRole('button', { name: 'Switch screen' }))
    await user.click(screen.getByRole('button', { name: 'Views' }))
    expect(await screen.findByText('Funnel view')).toBeInTheDocument()
    expect(screen.queryByText('Event view')).not.toBeInTheDocument()

    await user.click(screen.getByText('Save current…'))
    await user.type(screen.getByRole('textbox', { name: 'View name' }), 'Fresh funnel')
    await user.click(screen.getByRole('button', { name: 'Save' }))

    expect(JSON.parse(localStorage.getItem('apdl-admin:views:demo:funnels') ?? 'null')).toEqual([
      ...funnelViews,
      { name: 'Fresh funnel', view: { query: 'current-demo-funnels' } },
    ])
    expect(JSON.parse(localStorage.getItem('apdl-admin:views:demo:events') ?? 'null')).toEqual(
      eventViews,
    )
  })
})

describe('SavedViews storage validation', () => {
  test.each([
    [
      'unknown saved-view fields',
      [{ name: 'Invalid view', view: { query: 'query' }, legacy_name: 'invalid' }],
    ],
    [
      'unknown screen-view fields',
      [{ name: 'Invalid view', view: { query: 'query', legacy_query: 'invalid' } }],
    ],
    ['malformed required fields', [{ name: 42, view: { query: 'query' } }]],
  ])('rejects %s', async (_caseName, storedViews) => {
    const user = userEvent.setup()
    localStorage.setItem('apdl-admin:views:demo:events', JSON.stringify(storedViews))

    renderSavedViews()
    await user.click(screen.getByRole('button', { name: 'Views' }))

    expect(await screen.findByText('None yet')).toBeInTheDocument()
    expect(screen.queryByText('Invalid view')).not.toBeInTheDocument()
  })
})
