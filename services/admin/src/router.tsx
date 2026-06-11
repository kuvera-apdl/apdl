import { createBrowserRouter, Navigate, Outlet } from 'react-router-dom'

import { AppShell } from '@/components/layout/AppShell'
import { EmptyState } from '@/components/shared/PanelStates'
import { useWorkspace } from '@/core/workspace'
import { CohortsPage } from '@/features/analytics/CohortsPage'
import { EventsExplorerPage } from '@/features/analytics/EventsExplorerPage'
import { FunnelsPage } from '@/features/analytics/FunnelsPage'
import { RetentionPage } from '@/features/analytics/RetentionPage'
import { RunMonitorPage } from '@/features/agents/RunMonitorPage'
import { RunsPage } from '@/features/agents/RunsPage'
import { TriggerPage } from '@/features/agents/TriggerPage'
import {
  ExperimentCreatePage,
  ExperimentDetailPage,
} from '@/features/experiments/ExperimentDetailPage'
import { ExperimentListPage } from '@/features/experiments/ExperimentListPage'
import { FlagEditorPage } from '@/features/flags/editor/FlagEditorPage'
import { VerificationPage } from '@/features/verify/VerificationPage'
import { FlagDetailPage } from '@/features/flags/FlagDetailPage'
import { FlagListPage } from '@/features/flags/FlagListPage'
import { HygienePage } from '@/features/flags/HygienePage'
import { OverviewPage } from '@/features/overview/OverviewPage'
import { WorkspaceSettingsPage } from '@/features/settings/WorkspaceSettingsPage'
import { HealthPage } from '@/features/system/HealthPage'

// First-run gate (plan §5.1): no workspace → settings.
function RequireWorkspace() {
  const { active } = useWorkspace()
  if (!active) return <Navigate to="/settings/workspace" replace />
  return <Outlet />
}

function NotFoundPage() {
  return <EmptyState title="Page not found" description="This route does not exist." />
}

export function createRouter() {
  return createBrowserRouter([
    {
      element: <AppShell />,
      children: [
        { path: '/settings/workspace', element: <WorkspaceSettingsPage /> },
        {
          element: <RequireWorkspace />,
          children: [
            { path: '/', element: <OverviewPage /> },
            { path: '/flags', element: <FlagListPage /> },
            { path: '/flags/new', element: <FlagEditorPage /> },
            { path: '/flags/hygiene', element: <HygienePage /> },
            { path: '/flags/:key', element: <FlagDetailPage /> },
            { path: '/flags/:key/edit', element: <FlagEditorPage /> },
            { path: '/analytics/events', element: <EventsExplorerPage /> },
            { path: '/analytics/funnels', element: <FunnelsPage /> },
            { path: '/analytics/retention', element: <RetentionPage /> },
            { path: '/analytics/cohorts', element: <CohortsPage /> },
            { path: '/experiments', element: <ExperimentListPage /> },
            { path: '/experiments/new', element: <ExperimentCreatePage /> },
            { path: '/experiments/:key', element: <ExperimentDetailPage /> },
            { path: '/agents', element: <RunsPage /> },
            { path: '/agents/trigger', element: <TriggerPage /> },
            { path: '/agents/runs/:runId', element: <RunMonitorPage /> },
            { path: '/settings/verify', element: <VerificationPage /> },
            { path: '/system/health', element: <HealthPage /> },
          ],
        },
        { path: '*', element: <NotFoundPage /> },
      ],
    },
  ])
}
