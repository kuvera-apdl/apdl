import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
import { runInNewContext } from 'node:vm';
import replace from '@rollup/plugin-replace';
import { rollup } from 'rollup';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { CLIENT_KEY, ENDPOINT } from '../helpers';

describe('Next.js environment inlining', () => {
  it('resolves public config in a browser without a process global', async () => {
    const source = await readFile(resolve(process.cwd(), 'src/core/env.ts'), 'utf8');
    const javascript = ts.transpileModule(source, {
      compilerOptions: {
        module: ts.ModuleKind.ESNext,
        target: ts.ScriptTarget.ES2020,
      },
    }).outputText;

    const bundle = await rollup({
      input: 'virtual:env',
      plugins: [
        {
          name: 'virtual-env-module',
          resolveId(id) {
            return id === 'virtual:env' ? id : null;
          },
          load(id) {
            return id === 'virtual:env' ? javascript : null;
          },
        },
        replace({
          preventAssignment: true,
          values: {
            'process.env.NEXT_PUBLIC_APDL_URL': JSON.stringify(ENDPOINT),
            'process.env.NEXT_PUBLIC_APDL_CLIENT_KEY': JSON.stringify(CLIENT_KEY),
          },
        }),
      ],
    });
    const generated = await bundle.generate({
      format: 'iife',
      name: 'APDLEnv',
    });
    const chunk = generated.output[0];
    if (chunk.type !== 'chunk') {
      throw new Error('Expected Rollup to generate a JavaScript chunk');
    }

    const browserContext: {
      APDLEnv?: {
        endpointFromEnv(): string | undefined;
        clientKeyFromEnv(): string | undefined;
      };
    } = {};
    runInNewContext(chunk.code, browserContext);

    expect(browserContext.APDLEnv?.endpointFromEnv()).toBe(ENDPOINT);
    expect(browserContext.APDLEnv?.clientKeyFromEnv()).toBe(CLIENT_KEY);
  });
});
