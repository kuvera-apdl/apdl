import { readFileSync } from 'node:fs';
import { defineConfig } from 'rollup';
import typescript from '@rollup/plugin-typescript';
import resolve from '@rollup/plugin-node-resolve';
import terser from '@rollup/plugin-terser';
import replace from '@rollup/plugin-replace';

// Inject the version from package.json so it is the single source of truth.
const pkg = JSON.parse(readFileSync('./package.json', 'utf8'));

const injectVersion = replace({
  preventAssignment: true,
  values: {
    __APDL_SDK_VERSION__: JSON.stringify(pkg.version),
  },
});

// React is a peer dependency, and the core SDK is consumed via its own entry —
// never bundle either into the adapter.
const reactExternals = [
  'react',
  'react-dom',
  /^react\//,
  /^react-dom\//,
  '@apdl-oss/sdk',
];

export default defineConfig([
  {
    input: 'src/index.ts',
    output: [
      {
        file: 'dist/apdl.esm.js',
        format: 'es',
        sourcemap: true,
      },
      {
        file: 'dist/apdl.cjs',
        format: 'cjs',
        sourcemap: true,
        exports: 'named',
      },
      {
        file: 'dist/apdl.iife.js',
        format: 'iife',
        name: 'APDL',
        sourcemap: true,
        plugins: [terser()],
      },
    ],
    plugins: [
      resolve(),
      typescript({
        tsconfig: './tsconfig.json',
        declaration: true,
        declarationDir: 'dist',
      }),
      injectVersion,
    ],
  },
  {
    input: 'src/react/index.ts',
    external: reactExternals,
    output: [
      {
        file: 'dist/react.esm.js',
        format: 'es',
        sourcemap: true,
        banner: "'use client';",
      },
      {
        file: 'dist/react.cjs',
        format: 'cjs',
        sourcemap: true,
        exports: 'named',
        banner: "'use client';",
      },
    ],
    plugins: [
      resolve(),
      typescript({
        tsconfig: './tsconfig.json',
        declaration: true,
        declarationDir: 'dist',
      }),
      injectVersion,
    ],
  },
]);
