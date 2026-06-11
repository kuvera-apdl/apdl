import { describe, expect, test } from 'vitest'

import { diffObjects } from '../../src/components/shared/JsonDiff'

describe('diffObjects', () => {
  test('reports only changed keys', () => {
    const entries = diffObjects(
      { name: 'Old', version: 2, state: 'active' },
      { name: 'New', version: 3, state: 'active' },
    )
    expect(entries).toEqual([
      { key: 'name', kind: 'changed', before: 'Old', after: 'New' },
      { key: 'version', kind: 'changed', before: 2, after: 3 },
    ])
  })

  test('classifies added and removed keys', () => {
    const entries = diffObjects({ gone: 1 }, { fresh: 2 })
    expect(entries).toEqual([
      { key: 'fresh', kind: 'added', before: undefined, after: 2 },
      { key: 'gone', kind: 'removed', before: 1, after: undefined },
    ])
  })

  test('treats a null before (creation) as all-added', () => {
    const entries = diffObjects(null, { key: 'x' })
    expect(entries).toEqual([{ key: 'key', kind: 'added', before: undefined, after: 'x' }])
  })

  test('deep-compares nested values', () => {
    const entries = diffObjects(
      { variants: [{ key: 'control', weight: 1 }] },
      { variants: [{ key: 'control', weight: 2 }] },
    )
    expect(entries).toHaveLength(1)
    expect(entries[0]?.kind).toBe('changed')
  })

  test('returns nothing for identical objects', () => {
    expect(diffObjects({ a: 1 }, { a: 1 })).toEqual([])
  })
})
