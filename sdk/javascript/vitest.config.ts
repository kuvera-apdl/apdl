import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vitest/config';

// Resolve the self-referential package import to source (not the built dist),
// so the React adapter and the tests share one module instance under test.
const sdkEntry = fileURLToPath(new URL('./src/index.ts', import.meta.url));

// Inject the version from package.json (the single source of truth) so source
// under test reports the same version the built bundle ships.
const { version } = JSON.parse(
  readFileSync(new URL('./package.json', import.meta.url), 'utf8')
) as { version: string };

export default defineConfig({
  define: {
    __APDL_SDK_VERSION__: JSON.stringify(version),
  },
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
