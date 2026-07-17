import { expect, test } from 'vitest'

import { createRouter } from '../../src/router'

test('keeps auth and layout eager while resolving every screen through a lazy route module', async () => {
  window.history.replaceState(null, '', '/not-found')
  const router = createRouter()

  try {
    const authRoute = router.routes[2]
    const appShellRoute = authRoute?.children?.[0]
    const screenRoutes = [...router.routes.slice(0, 2), ...(appShellRoute?.children ?? [])]
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
