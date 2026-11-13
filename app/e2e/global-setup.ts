import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';

/** Build once, so the tests run against exactly what would ship (set MUSE_E2E_SKIP_BUILD=1 to reuse dist/). */
export default function globalSetup(): void {
  if (process.env.MUSE_E2E_SKIP_BUILD) return;
  const appDir = resolve(import.meta.dirname, '..');
  execFileSync(process.execPath, [resolve(appDir, 'node_modules/vite/bin/vite.js'), 'build'], { cwd: appDir, stdio: 'inherit' });
}
