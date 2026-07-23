import { expect, test } from 'vitest'

import { createRouter } from '../../src/router'

function descendants(routes: ReturnType<typeof createRouter>['routes']): ReturnType<typeof createRouter>['routes'] {
  return routes.flatMap((route) => [route, ...descendants(route.children ?? [])])
}

test('keeps auth and layout eager while resolving every screen through a lazy route module', async () => {
  window.history.replaceState(null, '', '/not-found')
  const router = createRouter()

  try {
    const authRoute = router.routes[2]
    const appShellRoute = authRoute?.children?.[0]
    const screenRoutes = [
      ...router.routes.slice(0, 2),
      ...descendants(appShellRoute?.children ?? []),
    ]
    const lazyRoutes = screenRoutes.filter((route) => route.lazy)

    expect(authRoute?.lazy).toBeUndefined()
    expect(appShellRoute?.lazy).toBeUndefined()
    expect(lazyRoutes).toHaveLength(30)

    for (const route of lazyRoutes) {
      if (!route.lazy) throw new Error(`Expected ${route.path ?? 'pathless route'} to be lazy`)
      const routeModule = await route.lazy()
      if (!('Component' in routeModule)) {
        throw new Error(`Expected ${route.path ?? 'pathless route'} to export a route component`)
      }
      expect(routeModule.Component, route.path).toBeTypeOf('function')
    }
  } finally {
    router.dispose()
  }
})

test('places every direct mutation screen behind its exact workspace role', () => {
  window.history.replaceState(null, '', '/not-found')
  const router = createRouter()

  try {
    const appRoutes = router.routes[2]?.children?.[0]?.children ?? []
    const protectedGroups = appRoutes.filter((route) => route.path === undefined && route.children)
    const roleByPath = new Map<string, unknown>()
    for (const group of protectedGroups) {
      const element = 'element' in group ? group.element : null
      const role = (element as { props?: { role?: unknown } } | null)?.props?.role
      for (const route of group.children ?? []) {
        if (route.path) roleByPath.set(route.path, role)
      }
    }

    expect(roleByPath).toEqual(new Map([
      ['/flags/new', 'config:write'],
      ['/flags/:key/edit', 'config:write'],
      ['/experiments/new', 'config:write'],
      ['/agents/trigger', 'agents:run'],
      ['/agents/custom/new', 'agents:manage'],
      ['/agents/custom/:agentId/edit', 'agents:manage'],
    ]))
  } finally {
    router.dispose()
  }
})
