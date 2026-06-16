import { defineConfig } from 'rollup';
import typescript from '@rollup/plugin-typescript';
import resolve from '@rollup/plugin-node-resolve';
import terser from '@rollup/plugin-terser';

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
        file: 'dist/apdl.cjs.js',
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
      },
      {
        file: 'dist/react.cjs.js',
        format: 'cjs',
        sourcemap: true,
        exports: 'named',
      },
    ],
    plugins: [
      resolve(),
      typescript({
        tsconfig: './tsconfig.json',
        declaration: true,
        declarationDir: 'dist',
      }),
    ],
  },
]);
