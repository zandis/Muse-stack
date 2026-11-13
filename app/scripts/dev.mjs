// `pnpm dev`: the fake home plus the Vite dev server that proxies /v1 to it.
//
//   pnpm dev                       a normal day
//   MUSE_FAKE_STATE=cards pnpm dev normal | used | paused | cards | empty | error
//
// To point the dev server at a real home started with `muse home serve --tcp 4181`,
// run Vite yourself: MUSE_HOME_PORT=4181 MUSE_HOME_TOKEN=<device token> pnpm exec vite
import { spawn } from 'node:child_process';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { FAKE_TOKEN, startFakeHome } from './fake-home.mjs';

const appDir = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const state = process.env.MUSE_FAKE_STATE || 'normal';
const home = await startFakeHome({ state });
console.log(`fake home: 127.0.0.1:${home.port} (state: ${state})`);

const vite = spawn(process.execPath, [resolve(appDir, 'node_modules/vite/bin/vite.js'), ...process.argv.slice(2)], {
  cwd: appDir,
  stdio: 'inherit',
  env: { ...process.env, MUSE_HOME_PORT: String(home.port), MUSE_HOME_TOKEN: FAKE_TOKEN },
});

let stopping = false;
async function stop(code) {
  if (stopping) return;
  stopping = true;
  vite.kill();
  await home.stop();
  process.exit(code);
}
vite.once('exit', (code) => void stop(code ?? 0));
process.on('SIGINT', () => void stop(0));
process.on('SIGTERM', () => void stop(0));
