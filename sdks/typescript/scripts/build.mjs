/**
 * Build the dual ESM + CJS package.
 *
 * Three `tsc` passes rather than a bundler, because the package has no runtime
 * dependencies and nothing to bundle. The only non-obvious step is the
 * `package.json` marker written into each output directory: the root package is
 * `"type": "module"`, so without `{"type":"commonjs"}` beside it Node would
 * read `dist/cjs/*.js` as ESM and every `require()` would fail.
 */

import { execFileSync } from 'node:child_process';
import { mkdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));

function run(label, args) {
  process.stdout.write(`  ${label}\n`);
  execFileSync(process.execPath, [join(root, 'node_modules', 'typescript', 'bin', 'tsc'), ...args], {
    cwd: root,
    stdio: 'inherit',
  });
}

process.stdout.write('building @fulcrum-ops/sdk\n');
rmSync(join(root, 'dist'), { recursive: true, force: true });

run('esm    ', ['-p', 'tsconfig.esm.json']);
run('cjs    ', ['-p', 'tsconfig.cjs.json']);
run('types  ', ['-p', 'tsconfig.types.json']);

for (const [directory, type] of [
  ['esm', 'module'],
  ['cjs', 'commonjs'],
]) {
  const target = join(root, 'dist', directory);
  mkdirSync(target, { recursive: true });
  writeFileSync(join(target, 'package.json'), `${JSON.stringify({ type }, null, 2)}\n`, 'utf8');
}

process.stdout.write('  done\n');
