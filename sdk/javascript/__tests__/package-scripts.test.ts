import { describe, expect, it } from 'vitest';
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { SDK_VERSION } from '../src/core/constants';

interface PackageJson {
  version?: string;
  files?: string[];
  scripts?: Record<string, string>;
}

const packageJson = JSON.parse(
  readFileSync(resolve(process.cwd(), 'package.json'), 'utf8')
) as PackageJson;

describe('package workflow scripts', () => {
  it('defines the SDK setup, validation, build, and package scripts', () => {
    expect(packageJson.scripts).toMatchObject({
      setup: 'npm ci',
      clean: 'rm -rf dist',
      build: 'rollup -c rollup.config.ts --configPlugin typescript',
      test: 'vitest run',
      typecheck: 'tsc --noEmit',
      lint: 'npm run typecheck && tsc -p __tests__/tsconfig.json --noEmit',
      'pack:dry-run': 'npm pack --dry-run',
      prepack: 'npm run build',
    });
  });

  it('runs lint, tests, build, and package dry-run during release validation', () => {
    expect(packageJson.scripts?.['release:check'])
      .toBe('npm run lint && npm test && npm run build && npm run pack:dry-run');
  });

  it('packages the built dist artifacts only', () => {
    expect(packageJson.files).toEqual(['dist']);
  });

  it('keeps the SDK version constant in sync with package.json', () => {
    expect(SDK_VERSION).toBe(packageJson.version);
  });
});
