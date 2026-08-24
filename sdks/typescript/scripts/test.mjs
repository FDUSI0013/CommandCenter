/**
 * Compile the tests alongside the sources and run them on the built-in runner.
 *
 * Tests are compiled to CommonJS rather than executed through a loader hook so
 * `npm test` works on any Node 18+ without experimental flags. That also means
 * the tests exercise the same module graph the CJS half of the package ships.
 *
 * Two details are load-bearing and both are invisible until they break:
 *
 * * The root `package.json` says `"type": "module"`, which Node applies to
 *   every `.js` file beneath it. Without a `{"type":"commonjs"}` marker inside
 *   `.test-build`, Node reads the compiled CJS output as ESM and every file
 *   dies on `exports is not defined`.
 * * The test files are passed to `node --test` individually rather than as a
 *   directory. Directory arguments are interpreted inconsistently across the
 *   Node versions this package supports; a list of files is not.
 */

import { execFileSync } from 'node:child_process';
import { mkdirSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const outDir = join(root, '.test-build');
const compiledTests = join(outDir, 'test');

process.stdout.write('compiling tests\n');
rmSync(outDir, { recursive: true, force: true });
execFileSync(process.execPath, [join(root, 'node_modules', 'typescript', 'bin', 'tsc'), '-p', 'tsconfig.test.json'], {
  cwd: root,
  stdio: 'inherit',
});

// Tell Node the compiled output is CommonJS, in spite of the ESM root package.
mkdirSync(outDir, { recursive: true });
writeFileSync(join(outDir, 'package.json'), `${JSON.stringify({ type: 'commonjs' }, null, 2)}\n`, 'utf8');

const files = readdirSync(compiledTests)
  .filter((name) => name.endsWith('.test.js'))
  .sort()
  .map((name) => join(compiledTests, name));

if (files.length === 0) {
  process.stderr.write('no compiled test files were found in .test-build/test\n');
  process.exit(1);
}

process.stdout.write(`running ${files.length} test file(s)\n`);
execFileSync(process.execPath, ['--test', ...files], { cwd: root, stdio: 'inherit' });
