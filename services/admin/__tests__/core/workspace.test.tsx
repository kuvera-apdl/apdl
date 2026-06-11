import { act, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { beforeEach, describe, expect, test } from 'vitest'

import { projectIdFromKey, useWorkspace, WorkspaceProvider } from '../../src/core/workspace'
import { makeWorkspace } from '../helpers/fixtures'

const wrapper = ({ children }: { children: ReactNode }) => (
  <WorkspaceProvider>{children}</WorkspaceProvider>
)

beforeEach(() => {
  localStorage.clear()
})

describe('projectIdFromKey', () => {
  test('derives the project id from a canonical key', () => {
    expect(projectIdFromKey('proj_demo_0123456789abcdef')).toBe('demo')
  })

  test('rejects malformed keys (short secret, wrong prefix)', () => {
    expect(projectIdFromKey('proj_demo_short')).toBeNull()
    expect(projectIdFromKey('key_demo_0123456789abcdef')).toBeNull()
    expect(projectIdFromKey('')).toBeNull()
  })
})

describe('WorkspaceProvider', () => {
  test('saves, activates, and persists workspaces', () => {
    const { result, unmount } = renderHook(() => useWorkspace(), { wrapper })
    expect(result.current.active).toBeNull()

    act(() => result.current.saveWorkspace(makeWorkspace()))
    expect(result.current.active?.name).toBe('Test')
    expect(result.current.projectId).toBe('demo')

    // A fresh provider (new session) loads the persisted state.
    unmount()
    const second = renderHook(() => useWorkspace(), { wrapper })
    expect(second.result.current.active?.id).toBe('ws-test')
  })

  test('updates an existing workspace in place', () => {
    const { result } = renderHook(() => useWorkspace(), { wrapper })
    act(() => result.current.saveWorkspace(makeWorkspace()))
    act(() => result.current.saveWorkspace(makeWorkspace({ name: 'Renamed' })))
    expect(result.current.workspaces).toHaveLength(1)
    expect(result.current.active?.name).toBe('Renamed')
  })

  test('deleting the active workspace falls back to the next one', () => {
    const { result } = renderHook(() => useWorkspace(), { wrapper })
    act(() => result.current.saveWorkspace(makeWorkspace({ id: 'a', name: 'A' })))
    act(() => result.current.saveWorkspace(makeWorkspace({ id: 'b', name: 'B' })))
    expect(result.current.active?.id).toBe('b')

    act(() => result.current.deleteWorkspace('b'))
    expect(result.current.active?.id).toBe('a')

    act(() => result.current.deleteWorkspace('a'))
    expect(result.current.active).toBeNull()
  })

  test('ignores corrupted persisted state', () => {
    localStorage.setItem('apdl-admin:workspaces', '{not json')
    const { result } = renderHook(() => useWorkspace(), { wrapper })
    expect(result.current.workspaces).toEqual([])
  })
})
