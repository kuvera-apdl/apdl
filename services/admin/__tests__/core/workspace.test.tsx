import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, test } from 'vitest'

import {
  serviceBaseUrl,
  serviceConnection,
  useWorkspace,
  WorkspaceProvider,
} from '../../src/core/workspace'
import { makeWorkspace } from '../helpers/fixtures'

const projects = [
  makeWorkspace(),
  makeWorkspace({ id: 'alpha', name: 'alpha', projectId: 'alpha' }),
]
const wrapper = ({ children }: { children: ReactNode }) => (
  <WorkspaceProvider initialWorkspaces={projects}>{children}</WorkspaceProvider>
)

beforeEach(() => {
  localStorage.clear()
})

describe('WorkspaceProvider', () => {
  test('selects only injected authorized projects and persists a non-secret project id', () => {
    const { result } = renderHook(() => useWorkspace(), { wrapper })
    expect(result.current.active?.id).toBe('demo')
    expect(result.current.projectId).toBe('demo')

    act(() => result.current.setActive('alpha'))
    expect(result.current.active?.id).toBe('alpha')
    expect(localStorage.getItem('apdl-admin:active-project')).toBe('alpha')

    act(() => result.current.setActive('not-authorized'))
    expect(result.current.active?.id).toBe('alpha')
  })

  test('builds same-origin service routes without credentials', () => {
    const workspace = projects[0]!
    expect(serviceBaseUrl(workspace, 'config')).toBe('/api/projects/demo/config')
    expect(serviceConnection(workspace, 'agents')).toEqual({
      baseUrl: '/api/projects/demo/agents',
      actor: 'tester@example.com',
    })
  })
})
