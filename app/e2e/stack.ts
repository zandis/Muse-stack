// One "stack" per test need: a fake home in a given state on a free port, and
// `vite preview` serving dist/ on another free port with its proxy pointed at it.
import { spawn } from 'node:child_process';
import { resolve } from 'node:path';
import AxeBuilder from '@axe-core/playwright';
import { test as base, expect, type Page } from '@playwright/test';
import { callHome, FAKE_TOKEN, freePort, startFakeHome } from '../scripts/fake-home.mjs';

export type FakeState = 'normal' | 'used' | 'paused' | 'cards' | 'empty' | 'error' | 'mismatch';
export const STATES: readonly FakeState[] = ['normal', 'used', 'paused', 'cards', 'empty', 'error'];
export const PHONE = { width: 390, height: 844 };
export const DESKTOP = { width: 1280, height: 800 };

const APP_DIR = resolve(import.meta.dirname, '..');

export interface Stack {
  url: string;
  homePort: number;
  /** Ask the fake home directly, from Node, to check what the app really changed. */
  home: (method: string, path: string, body?: unknown) => Promise<{ status: number; body: any }>; // eslint-disable-line @typescript-eslint/no-explicit-any
  stopHome: () => Promise<void>;
  stop: () => Promise<void>;
}

export async function startStack(state: FakeState): Promise<Stack> {
  const home = await startFakeHome({ state });
  const port = await freePort();
  const url = `http://127.0.0.1:${port}`;
  const preview = spawn(process.execPath, [resolve(APP_DIR, 'node_modules/vite/bin/vite.js'), 'preview', '--port', String(port), '--strictPort'], {
    cwd: APP_DIR,
    env: { ...process.env, MUSE_HOME_PORT: String(home.port), MUSE_HOME_TOKEN: FAKE_TOKEN, VITE_CONFIG_NATIVE_IGNORE_WARNING: 'true' },
    stdio: ['ignore', 'ignore', 'pipe'],
  });
  let err = '';
  preview.stderr.on('data', (d) => {
    err = (err + String(d)).slice(-2000);
  });
  const deadline = Date.now() + 20_000;
  for (;;) {
    try {
      if ((await fetch(url)).ok) break;
    } catch {
      /* not up yet */
    }
    if (preview.exitCode !== null || Date.now() > deadline) {
      preview.kill();
      await home.stop();
      throw new Error(`vite preview did not start: ${err}`);
    }
    await new Promise((r) => setTimeout(r, 100));
  }
  return {
    url,
    homePort: home.port,
    home: (method, path, body) => callHome(home.port, method, path, body),
    stopHome: () => home.stop(),
    stop: async () => {
      preview.kill();
      await home.stop();
    },
  };
}

/** What a page did that it should not have: console errors, page errors, requests to anywhere but the app itself. */
export interface Watch {
  problems: string[];
  /** Call for a state where the home answers 503 on purpose: the browser logs each one. */
  allowHomeFailures: () => void;
}

export function watch(page: Page, origin: string): Watch {
  const problems: string[] = [];
  let allowFailures = false;
  page.on('console', (msg) => {
    if (msg.type() !== 'error') return;
    const where = msg.location().url;
    if (allowFailures && msg.text().startsWith('Failed to load resource') && where.startsWith(`${origin}/v1/`)) return;
    problems.push(`console: ${msg.text()} (${where})`);
  });
  page.on('pageerror', (e) => problems.push(`pageerror: ${e.message}`));
  page.on('request', (req) => {
    const u = req.url();
    if (!u.startsWith(`${origin}/`) && !u.startsWith('data:') && !u.startsWith('about:')) problems.push(`request left the app: ${u}`);
  });
  return { problems, allowHomeFailures: () => (allowFailures = true) };
}

export async function noSidewaysScroll(page: Page): Promise<void> {
  const over = await page.evaluate(() => {
    const out: string[] = [];
    const vw = document.documentElement.clientWidth;
    if (document.documentElement.scrollWidth > vw) out.push(`document ${document.documentElement.scrollWidth} > ${vw}`);
    for (const el of document.querySelectorAll<HTMLElement>('main, nav, .scroll, .tray')) {
      if (el.scrollWidth > el.clientWidth + 1) out.push(`${el.className} ${el.scrollWidth} > ${el.clientWidth}`);
    }
    return out;
  });
  expect(over, 'nothing scrolls sideways').toEqual([]);
}

/** axe-core: no serious or critical accessibility violation. */
export async function axeClean(page: Page): Promise<void> {
  const result = await new AxeBuilder({ page }).withTags(['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa', 'best-practice']).analyze();
  const bad = result.violations
    .filter((v) => v.impact === 'serious' || v.impact === 'critical')
    .map((v) => `${v.id} (${v.impact}): ${v.nodes.map((n) => `${n.target.join(' ')} ${n.failureSummary ?? ''}`).join(' | ')}`);
  expect(bad, 'no serious or critical accessibility violations').toEqual([]);
}

/** An empty home whose first run was skipped: Today shows its empty state instead of opening /welcome. */
export async function skipFirstRun(stack: Stack): Promise<Stack> {
  const r = await stack.home('PATCH', '/v1/settings', { first_run: 'skipped' });
  if (r.status !== 200) throw new Error(`the fake home did not take first_run: ${r.status}`);
  return stack;
}

class Pool {
  private readonly stacks = new Map<FakeState, Promise<Stack>>();
  get(state: FakeState): Promise<Stack> {
    let s = this.stacks.get(state);
    if (!s) {
      // A shared empty home is one where "Skip for now" was pressed: the first run opens by itself only once.
      s = state === 'empty' ? startStack(state).then(skipFirstRun) : startStack(state);
      this.stacks.set(state, s);
    }
    return s;
  }
  async stop(): Promise<void> {
    for (const s of this.stacks.values()) await (await s).stop();
  }
}

export const test = base.extend<{ fresh: (state: FakeState) => Promise<Stack> }, { shared: (state: FakeState) => Promise<Stack> }>({
  /** A stack shared by the tests in this worker. Only for tests that change nothing on the home. */
  shared: [
    // eslint-disable-next-line no-empty-pattern
    async ({}, use) => {
      const pool = new Pool();
      await use((state) => pool.get(state));
      await pool.stop();
    },
    { scope: 'worker' },
  ],
  /** A stack of this test's own, for tests that answer cards, pause, or stop the home. */
  // eslint-disable-next-line no-empty-pattern
  fresh: async ({}, use) => {
    const made: Stack[] = [];
    await use(async (state) => {
      const s = await startStack(state);
      made.push(s);
      return s;
    });
    for (const s of made) await s.stop();
  },
});

export { expect };
