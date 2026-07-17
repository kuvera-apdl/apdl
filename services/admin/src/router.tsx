import type { ComponentType } from 'react'
import { createBrowserRouter, Navigate, Outlet, useLocation } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { EmptyState } from '@/components/shared/PanelStates'
import { useAuth } from '@/core/auth'

function lazyRoute<TExport extends string>(
  load: () => Promise<Record<TExport, ComponentType>>,
  exportName: TExport,
) {
  return async () => {
    const module = await load()
    return { Component: module[exportName] }
  }
}

export function RequireAuth() {
  const { authenticated, initializing } = useAuth()
  const location = useLocation()
  if (initializing) return null
  if (!authenticated) {
    const from = `${location.pathname}${location.search}${location.hash}`
    return <Navigate to="/login" replace state={{ from }} />
  }
  return <Outlet />
}

function NotFoundPage() {
  return <EmptyState title="Page not found" description="This route does not exist." />
}

export function createRouter() {
  return createBrowserRouter([
    {
      path: '/login',
      lazy: lazyRoute(() => import('@/features/auth/LoginPage'), 'LoginPage'),
    },
    {
      path: '/register',
      lazy: lazyRoute(() => import('@/features/auth/RegisterPage'), 'RegisterPage'),
    },
    {
      element: <RequireAuth />,
      children: [
        {
          element: <AppShell />,
          children: [
            {
              path: '/',
              lazy: lazyRoute(() => import('@/features/overview/OverviewPage'), 'OverviewPage'),
            },
            {
              path: '/decide',
              lazy: lazyRoute(() => import('@/features/loop/DecidePage'), 'DecidePage'),
            },
            {
              path: '/watch',
              lazy: lazyRoute(() => import('@/features/loop/WatchPage'), 'WatchPage'),
            },
            {
              path: '/learn',
              lazy: lazyRoute(() => import('@/features/loop/LearnPage'), 'LearnPage'),
            },
            {
              path: '/steer',
              lazy: lazyRoute(() => import('@/features/loop/SteerPage'), 'SteerPage'),
            },
            {
              path: '/flags',
              lazy: lazyRoute(() => import('@/features/flags/FlagListPage'), 'FlagListPage'),
            },
            {
              path: '/flags/new',
              lazy: lazyRoute(
                () => import('@/features/flags/editor/FlagEditorPage'),
                'FlagEditorPage',
              ),
            },
            {
              path: '/flags/hygiene',
              lazy: lazyRoute(() => import('@/features/flags/HygienePage'), 'HygienePage'),
            },
            {
              path: '/flags/:key',
              lazy: lazyRoute(() => import('@/features/flags/FlagDetailPage'), 'FlagDetailPage'),
            },
            {
              path: '/flags/:key/edit',
              lazy: lazyRoute(
                () => import('@/features/flags/editor/FlagEditorPage'),
                'FlagEditorPage',
              ),
            },
            {
              path: '/analytics/events',
              lazy: lazyRoute(
                () => import('@/features/analytics/EventsExplorerPage'),
                'EventsExplorerPage',
              ),
            },
            {
              path: '/analytics/funnels',
              lazy: lazyRoute(() => import('@/features/analytics/FunnelsPage'), 'FunnelsPage'),
            },
            {
              path: '/analytics/retention',
              lazy: lazyRoute(() => import('@/features/analytics/RetentionPage'), 'RetentionPage'),
            },
            {
              path: '/analytics/cohorts',
              lazy: lazyRoute(() => import('@/features/analytics/CohortsPage'), 'CohortsPage'),
            },
            {
              path: '/experiments',
              lazy: lazyRoute(
                () => import('@/features/experiments/ExperimentListPage'),
                'ExperimentListPage',
              ),
            },
            {
              path: '/experiments/new',
              lazy: lazyRoute(
                () => import('@/features/experiments/ExperimentDetailPage'),
                'ExperimentCreatePage',
              ),
            },
            {
              path: '/experiments/:key',
              lazy: lazyRoute(
                () => import('@/features/experiments/ExperimentDetailPage'),
                'ExperimentDetailPage',
              ),
            },
            {
              path: '/agents',
              lazy: lazyRoute(() => import('@/features/agents/RunsPage'), 'RunsPage'),
            },
            {
              path: '/agents/trigger',
              lazy: lazyRoute(() => import('@/features/agents/TriggerPage'), 'TriggerPage'),
            },
            {
              path: '/agents/custom',
              lazy: lazyRoute(
                () => import('@/features/agents/custom/CustomAgentsPage'),
                'CustomAgentsPage',
              ),
            },
            {
              path: '/agents/custom/new',
              lazy: lazyRoute(
                () => import('@/features/agents/custom/CustomAgentWizardPage'),
                'CustomAgentWizardPage',
              ),
            },
            {
              path: '/agents/custom/:agentId/edit',
              lazy: lazyRoute(
                () => import('@/features/agents/custom/CustomAgentWizardPage'),
                'CustomAgentWizardPage',
              ),
            },
            {
              path: '/agents/runs/:runId',
              lazy: lazyRoute(
                () => import('@/features/agents/RunMonitorPage'),
                'RunMonitorPage',
              ),
            },
            {
              path: '/codegen',
              lazy: lazyRoute(() => import('@/features/codegen/ChangesetsPage'), 'ChangesetsPage'),
            },
            {
              path: '/codegen/:id',
              lazy: lazyRoute(
                () => import('@/features/codegen/ChangesetDetailPage'),
                'ChangesetDetailPage',
              ),
            },
            {
              path: '/settings/workspace',
              lazy: lazyRoute(
                () => import('@/features/settings/WorkspaceSettingsPage'),
                'WorkspaceSettingsPage',
              ),
            },
            {
              path: '/settings/verify',
              lazy: lazyRoute(
                () => import('@/features/verify/VerificationPage'),
                'VerificationPage',
              ),
            },
            {
              path: '/system/health',
              lazy: lazyRoute(() => import('@/features/system/HealthPage'), 'HealthPage'),
            },
            { path: '*', element: <NotFoundPage /> },
          ],
        },
      ],
    },
  ])
}
