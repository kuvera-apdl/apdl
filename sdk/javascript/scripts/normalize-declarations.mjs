import { readdir, readFile, writeFile } from 'node:fs/promises';
import { join } from 'node:path';
import { fileURLToPath } from 'node:url';

const distDir = fileURLToPath(new URL('../dist/', import.meta.url));
const relativeFrom = /(\bfrom\s+['"])(\.{1,2}\/[^'"]+)(['"])/g;
const relativeImport = /(\bimport\s*\(\s*['"])(\.{1,2}\/[^'"]+)(['"]\s*\))/g;
const explicitExtension = /\.(?:[cm]?js|json)$/;

let rewriteCount = 0;

function normalizeSpecifier(_match, prefix, specifier, suffix) {
  if (explicitExtension.test(specifier)) return `${prefix}${specifier}${suffix}`;
  rewriteCount += 1;
  return `${prefix}${specifier}.js${suffix}`;
}

function commonJsSpecifier(_match, prefix, specifier, suffix) {
  const commonJs = specifier.endsWith('.js')
    ? `${specifier.slice(0, -3)}.cjs`
    : specifier;
  return `${prefix}${commonJs}${suffix}`;
}

async function declarationFiles(directory) {
  const files = [];
  for (const entry of await readdir(directory, { withFileTypes: true })) {
    const path = join(directory, entry.name);
    if (entry.isDirectory()) files.push(...await declarationFiles(path));
    else if (entry.isFile() && entry.name.endsWith('.d.ts')) files.push(path);
  }
  return files;
}

for (const path of await declarationFiles(distDir)) {
  const original = await readFile(path, 'utf8');
  const normalized = original
    .replace(relativeFrom, normalizeSpecifier)
    .replace(relativeImport, normalizeSpecifier);
  if (normalized !== original) await writeFile(path, normalized);

  const commonJsDeclaration = normalized
    .replace(relativeFrom, commonJsSpecifier)
    .replace(relativeImport, commonJsSpecifier);
  await writeFile(path.replace(/\.d\.ts$/, '.d.cts'), commonJsDeclaration);
}

if (rewriteCount === 0) {
  throw new Error('No relative declaration specifiers were normalized');
}
