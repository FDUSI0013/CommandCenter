/** Remove build and test output. */

import { rmSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const root = dirname(dirname(fileURLToPath(import.meta.url)));
for (const directory of ['dist', '.test-build']) {
  rmSync(join(root, directory), { recursive: true, force: true });
}
process.stdout.write('cleaned\n');
