import { cleanup, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, expect, test } from 'vitest'
import { MemoryRouter, Route, Routes } from 'react-router-dom'

import { WorkspaceProvider } from '../../src/core/workspace'
import { createRouter, RequireWorkspaceRole } from '../../src/router'
import { makeWorkspace } from '../helpers/fixtures'

afterEach(cleanup)

test('denies a protected route when the active workspace lacks its exact role', () => {
  render(
    <WorkspaceProvider
      initialWorkspaces={[makeWorkspace({ roles: ['config:read'] })]}
    >
      <MemoryRouter initialEntries={['/protected']}>
        <Routes>
          <Route element={<RequireWorkspaceRole role="config:write" />}>
            <Route path="/protected" element={<div>Protected content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </WorkspaceProvider>,
  )

  expect(screen.getByText('Action unavailable')).toBeInTheDocument()
  expect(
    screen.getByText(
      'This route requires config:write for the active project.',
    ),
  ).toBeInTheDocument()
  expect(screen.queryByText('Protected content')).not.toBeInTheDocument()
})

test('renders a protected route when the active workspace has its exact role', () => {
  render(
    <WorkspaceProvider
      initialWorkspaces={[makeWorkspace({ roles: ['config:write'] })]}
    >
      <MemoryRouter initialEntries={['/protected']}>
        <Routes>
          <Route element={<RequireWorkspaceRole role="config:write" />}>
            <Route path="/protected" element={<div>Protected content</div>} />
          </Route>
        </Routes>
      </MemoryRouter>
    </WorkspaceProvider>,
  )

  expect(screen.getByText('Protected content')).toBeInTheDocument()
  expect(screen.queryByText('Action unavailable')).not.toBeInTheDocument()
})

test('renders the application not-found boundary for an unknown route', () => {
  window.history.replaceState(null, '', '/not-found')
  const router = createRouter()

  try {
    const appRoutes =
      router.routes.find((route) => route.path === undefined)?.children?.[0]?.children ?? []
    const notFoundRoute = appRoutes.find((route) => route.path === '*')
    const notFoundElement =
      notFoundRoute && 'element' in notFoundRoute
        ? notFoundRoute.element
        : undefined

    expect(notFoundElement).toBeDefined()
    render(notFoundElement as ReactNode)
    expect(screen.getByText('Page not found')).toBeInTheDocument()
    expect(screen.getByText('This route does not exist.')).toBeInTheDocument()
  } finally {
    router.dispose()
  }
})
