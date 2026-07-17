import { fileURLToPath } from 'node:url'

import react from '@vitejs/plugin-react'
import { defineConfig } from 'vitest/config'

const isChangedCoverageRun = process.env.npm_lifecycle_event === 'test:coverage:changed'
const changedCoverageBase = isChangedCoverageRun
  ? process.env.COVERAGE_BASE?.trim() || 'origin/main'
  : undefined

export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['__tests__/setup.ts'],
    include: ['__tests__/**/*.test.ts', '__tests__/**/*.test.tsx'],
    css: false,
    coverage: {
      provider: 'v8',
      include: ['src/**/*.{ts,tsx}'],
      exclude: ['src/**/*.d.ts'],
      changed: changedCoverageBase,
      reportsDirectory: changedCoverageBase ? 'coverage/changed' : 'coverage/full',
      reporter: ['text', 'json-summary', 'html', 'lcov', 'cobertura'],
      reportOnFailure: true,
      thresholds: {
        // Separate baselines for the full source tree and files changed from
        // COVERAGE_BASE. Raise these floors as coverage improves.
        ...(changedCoverageBase
          ? {
              statements: 64,
              branches: 54,
              functions: 61,
              lines: 68,
            }
          : {
              statements: 72,
              branches: 61,
              functions: 67,
              lines: 74,
            }),
        // Release-critical authorization, navigation, and error boundaries.
        'src/api/http.ts': {
          statements: 92,
          branches: 85,
          functions: 100,
          lines: 93,
        },
        'src/core/auth.tsx': {
          statements: 84,
          branches: 31,
          functions: 91,
          lines: 88,
        },
        'src/core/workspace.tsx': {
          statements: 91,
          branches: 92,
          functions: 100,
          lines: 93,
        },
        'src/router.tsx': {
          statements: 97,
          branches: 100,
          functions: 97,
          lines: 97,
        },
      },
    },
  },
})
