import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Resolve the self-referential package import to source (not the built dist),
// so the React adapter and the tests share one module instance under test.
const sdkEntry = fileURLToPath(new URL('./src/index.ts', import.meta.url));

export default defineConfig({
  resolve: {
    alias: {
      '@apdl-oss/sdk': sdkEntry,
    },
  },
  test: {
    environment: 'jsdom',
    globals: true,
    include: ['__tests__/**/*.test.ts'],
    coverage: {
      provider: 'v8',
      include: ['src/**/*.ts'],
      exclude: ['src/index.ts'],
    },
  },
});
