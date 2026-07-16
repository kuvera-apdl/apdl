import {
  Activity,
  BadgeCheck,
  BarChart3,
  Bot,
  Check,
  ChevronsUpDown,
  Eye,
  Filter,
  Flag,
  FlaskConical,
  Gavel,
  GitPullRequest,
  Grid3x3,
  LayoutDashboard,
  Lightbulb,
  LogOut,
  Monitor,
  Moon,
  PanelLeftClose,
  PanelLeftOpen,
  Settings,
  SlidersHorizontal,
  Sun,
  Users,
  type LucideIcon,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, Outlet, useLocation, useNavigate } from 'react-router-dom'
import { toast } from 'sonner'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { useLive } from '@/core/live'
import { useAuth } from '@/core/auth'
import { useTheme, type Theme } from '@/core/theme'
import { useWorkspace } from '@/core/workspace'
import { useNow } from '@/lib/hooks'
import { cn } from '@/lib/utils'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  isActive: (path: string) => boolean
}

interface NavGroup {
  label: string | null
  items: NavItem[]
}

// Purpose-centric IA (admin-console-purpose-ia.md): Overview, then Loop
// (verbs — what you came to do), then Features (registries — where objects
// live), Analytics, and System last.
const NAV_GROUPS: NavGroup[] = [
  {
    label: null,
    items: [{ to: '/', label: 'Overview', icon: LayoutDashboard, isActive: (path) => path === '/' }],
  },
  {
    label: 'Loop',
    items: [
      { to: '/decide', label: 'Decide', icon: Gavel, isActive: (path) => path === '/decide' },
      { to: '/watch', label: 'Watch', icon: Eye, isActive: (path) => path === '/watch' },
      { to: '/learn', label: 'Learn', icon: Lightbulb, isActive: (path) => path === '/learn' },
      { to: '/steer', label: 'Steer', icon: SlidersHorizontal, isActive: (path) => path === '/steer' },
    ],
  },
  {
    label: 'Features',
    items: [
      {
        to: '/flags',
        label: 'Feature flags',
        icon: Flag,
        isActive: (path) => path === '/flags' || path.startsWith('/flags/'),
      },
      {
        to: '/experiments',
        label: 'Experiments',
        icon: FlaskConical,
        isActive: (path) => path === '/experiments' || path.startsWith('/experiments/'),
      },
      {
        to: '/agents',
        label: 'Agent runs',
        icon: Bot,
        isActive: (path) => path === '/agents' || path.startsWith('/agents/'),
      },
      {
        to: '/codegen',
        label: 'Codegen',
        icon: GitPullRequest,
        isActive: (path) => path === '/codegen' || path.startsWith('/codegen/'),
      },
    ],
  },
  {
    label: 'Analytics',
    items: [
      {
        to: '/analytics/events',
        label: 'Events',
        icon: BarChart3,
        isActive: (path) => path === '/analytics/events',
      },
      {
        to: '/analytics/funnels',
        label: 'Funnels',
        icon: Filter,
        isActive: (path) => path === '/analytics/funnels',
      },
      {
        to: '/analytics/retention',
        label: 'Retention',
        icon: Grid3x3,
        isActive: (path) => path === '/analytics/retention',
      },
      {
        to: '/analytics/cohorts',
        label: 'Cohorts',
        icon: Users,
        isActive: (path) => path === '/analytics/cohorts',
      },
    ],
  },
  {
    label: 'System',
    items: [
      {
        to: '/settings/workspace',
        label: 'Workspace',
        icon: Settings,
        isActive: (path) => path === '/settings/workspace',
      },
      {
        to: '/settings/verify',
        label: 'Verify integration',
        icon: BadgeCheck,
        isActive: (path) => path === '/settings/verify',
      },
      {
        to: '/system/health',
        label: 'System health',
        icon: Activity,
        isActive: (path) => path === '/system/health',
      },
    ],
  },
]

const SIDEBAR_KEY = 'apdl-admin:sidebar-collapsed'

function Sidebar() {
  const location = useLocation()
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem(SIDEBAR_KEY) === '1')

  useEffect(() => {
    localStorage.setItem(SIDEBAR_KEY, collapsed ? '1' : '0')
  }, [collapsed])

  return (
    <aside
      className={cn(
        'flex h-full shrink-0 flex-col border-r bg-card transition-[width]',
        collapsed ? 'w-14' : 'w-56',
      )}
    >
      <div className={cn('flex h-14 items-center border-b px-4', collapsed && 'justify-center px-0')}>
        <Link to="/" className="font-semibold tracking-tight">
          {collapsed ? 'A' : 'APDL Admin'}
        </Link>
      </div>
      <nav className="flex-1 space-y-3 overflow-y-auto p-2">
        {NAV_GROUPS.map((group, groupIndex) => (
          <div key={group.label ?? groupIndex} className="space-y-1">
            {group.label && !collapsed ? (
              <p className="px-2.5 pt-1 text-[11px] font-medium uppercase tracking-wide text-muted-foreground">
                {group.label}
              </p>
            ) : null}
            {group.items.map((item) => {
              const active = item.isActive(location.pathname)
              return (
                <Link
                  key={item.to}
                  to={item.to}
                  title={collapsed ? item.label : undefined}
                  className={cn(
                    'flex items-center gap-2.5 rounded-md px-2.5 py-2 text-sm font-medium transition-colors',
                    active
                      ? 'bg-accent text-accent-foreground'
                      : 'text-muted-foreground hover:bg-accent/60 hover:text-foreground',
                    collapsed && 'justify-center px-0',
                  )}
                >
                  <item.icon className="h-4 w-4 shrink-0" />
                  {collapsed ? null : item.label}
                </Link>
              )
            })}
          </div>
        ))}
      </nav>
      <div className="border-t p-2">
        <Button
          variant="ghost"
          size="icon"
          className="w-full text-muted-foreground"
          onClick={() => setCollapsed((value) => !value)}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
        >
          {collapsed ? <PanelLeftOpen /> : <PanelLeftClose />}
        </Button>
      </div>
    </aside>
  )
}

function WorkspaceSwitcher() {
  const { workspaces, active, setActive } = useWorkspace()
  const navigate = useNavigate()

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size="sm" className="gap-2">
          {active ? active.name : 'No workspace'}
          <ChevronsUpDown className="h-3.5 w-3.5 text-muted-foreground" />
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="start">
        <DropdownMenuLabel>Authorized projects</DropdownMenuLabel>
        {workspaces.map((workspace) => (
          <DropdownMenuItem key={workspace.id} onSelect={() => setActive(workspace.id)}>
            <Check className={cn('opacity-0', workspace.id === active?.id && 'opacity-100')} />
            {workspace.name}
          </DropdownMenuItem>
        ))}
        {workspaces.length > 0 ? <DropdownMenuSeparator /> : null}
        <DropdownMenuItem onSelect={() => navigate('/settings/workspace')}>
          View access…
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  )
}

const LIVE_LABELS = {
  idle: 'Offline',
  connecting: 'Connecting…',
  open: 'Live',
  reconnecting: 'Reconnecting…',
} as const

function LiveIndicator() {
  const { state } = useLive()
  const now = useNow(5000)
  const secondsAgo =
    state.lastEventAt !== null ? Math.max(0, Math.round((now - state.lastEventAt) / 1000)) : null

  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-default items-center gap-1.5 text-sm text-muted-foreground">
          <span
            className={cn(
              'h-2 w-2 rounded-full',
              state.status === 'open' && 'bg-emerald-500',
              (state.status === 'connecting' || state.status === 'reconnecting') &&
                'animate-pulse bg-amber-500',
              state.status === 'idle' && 'bg-muted-foreground/40',
            )}
          />
          {LIVE_LABELS[state.status]}
        </span>
      </TooltipTrigger>
      <TooltipContent>
        SSE stream — {LIVE_LABELS[state.status].toLowerCase()}
        {secondsAgo !== null ? ` · last event ${secondsAgo}s ago` : ''}
        {state.reconnects > 0 ? ` · ${state.reconnects} reconnects` : ''}
      </TooltipContent>
    </Tooltip>
  )
}

/** Server-time clock — the analytics pipeline buckets every timestamp in UTC. */
function UtcClock() {
  const now = useNow(30_000)
  const date = new Date(now)
  const label = `${String(date.getUTCHours()).padStart(2, '0')}:${String(date.getUTCMinutes()).padStart(2, '0')} UTC`
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span className="inline-flex cursor-default items-center text-sm tabular-nums text-muted-foreground">
          {label}
        </span>
      </TooltipTrigger>
      <TooltipContent>Server time — analytics are bucketed in UTC</TooltipContent>
    </Tooltip>
  )
}

function ThemeToggle() {
  const { theme, setTheme } = useTheme()
  const next: Record<Theme, Theme> = { light: 'dark', dark: 'system', system: 'light' }
  const Icon = theme === 'light' ? Sun : theme === 'dark' ? Moon : Monitor
  return (
    <Button
      variant="ghost"
      size="icon"
      onClick={() => setTheme(next[theme])}
      aria-label={`Theme: ${theme}`}
      title={`Theme: ${theme}`}
    >
      <Icon />
    </Button>
  )
}

export function AppShell() {
  const { projectId } = useWorkspace()
  const { identity, logout } = useAuth()

  return (
    <div className="flex h-screen overflow-hidden">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-14 shrink-0 items-center justify-between gap-4 border-b px-4">
          <div className="flex items-center gap-3">
            <WorkspaceSwitcher />
            {projectId ? (
              <Badge variant="secondary" className="font-mono">
                project: {projectId}
              </Badge>
            ) : null}
          </div>
          <div className="flex items-center gap-3">
            <UtcClock />
            <LiveIndicator />
            {identity ? (
              <span className="hidden text-sm text-muted-foreground md:inline">
                <span className="text-foreground">{identity.email}</span>
              </span>
            ) : null}
            <Button
              variant="ghost"
              size="icon"
              onClick={() => void logout().catch(() => toast.error('Unable to revoke the session'))}
              aria-label="Sign out"
              title="Sign out"
            >
              <LogOut />
            </Button>
            <ThemeToggle />
          </div>
        </header>
        <main className="min-h-0 flex-1 overflow-auto">
          <div className="mx-auto w-full max-w-7xl space-y-6 p-6">
            <Outlet />
          </div>
        </main>
      </div>
    </div>
  )
}
