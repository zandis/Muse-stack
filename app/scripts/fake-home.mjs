// Starts the fake home (python -m muse.home --fake) from this repository on
// 127.0.0.1. Used by `pnpm dev` and by the end-to-end tests.
//
//   MUSE_PYTHON   the Python to use (default: python3). It needs this repository's
//                 Python dependencies, for example a virtualenv with `pip install -e .`.
import { spawn } from 'node:child_process';
import { createServer } from 'node:net';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

export const FAKE_TOKEN = 'fake-home-token'; // fixed, and only ever works on a fake home
export const STATES = ['normal', 'used', 'paused', 'cards', 'empty', 'error', 'mismatch'];
export const REPO_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..', '..');

export function freePort() {
  return new Promise((ok, fail) => {
    const srv = createServer();
    srv.once('error', fail);
    srv.listen(0, '127.0.0.1', () => {
      const { port } = srv.address();
      srv.close(() => ok(port));
    });
  });
}

/** Call the fake home directly (Node side only; the browser never does this). */
export async function callHome(port, method, path, body) {
  const res = await fetch(`http://127.0.0.1:${port}${path}`, {
    method,
    headers: { Authorization: `Bearer ${FAKE_TOKEN}`, ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: JSON.stringify(body) }),
  });
  return { status: res.status, body: await res.json().catch(() => null) };
}

export async function startFakeHome({ state = 'normal', port, quiet = true } = {}) {
  if (!STATES.includes(state)) throw new Error(`state must be one of ${STATES.join(', ')}`);
  const usePort = port ?? (await freePort());
  const python = process.env.MUSE_PYTHON || 'python3';
  const child = spawn(python, ['-m', 'muse.home', '--fake', '--tcp', String(usePort), '--state', state], {
    cwd: REPO_ROOT,
    env: { ...process.env, PYTHONPATH: [REPO_ROOT, process.env.PYTHONPATH].filter(Boolean).join(':'), PYTHONDONTWRITEBYTECODE: '1' },
    stdio: quiet ? ['ignore', 'ignore', 'pipe'] : 'inherit',
  });
  let stderr = '';
  child.stderr?.on('data', (d) => {
    stderr = (stderr + d).slice(-4000);
  });
  let exited = null;
  child.once('exit', (code) => {
    exited = code ?? -1;
  });
  const deadline = Date.now() + 20_000;
  for (;;) {
    if (exited !== null) throw new Error(`the fake home stopped (exit ${exited}). Is MUSE_PYTHON set to a Python with this repository installed?\n${stderr}`);
    try {
      const r = await callHome(usePort, 'GET', '/v1/status');
      if (r.status === 200 || r.status === 503) break;
    } catch {
      /* not listening yet */
    }
    if (Date.now() > deadline) {
      child.kill();
      throw new Error(`the fake home did not start within 20 seconds\n${stderr}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return {
    port: usePort,
    state,
    stop: () =>
      new Promise((ok) => {
        if (exited !== null) return ok();
        child.once('exit', () => ok());
        child.kill();
      }),
  };
}
