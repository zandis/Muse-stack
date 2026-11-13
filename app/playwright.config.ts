import { defineConfig } from '@playwright/test';

// End to end: the built app served by `vite preview` (which proxies /v1), against the
// fake home from this repository in each of its states. See e2e/stack.ts.
//
// Chromium is taken from PLAYWRIGHT_BROWSERS_PATH when it is set; otherwise run
// `pnpm exec playwright install chromium` once.
export default defineConfig({
  testDir: 'e2e',
  globalSetup: './e2e/global-setup.ts',
  fullyParallel: true,
  workers: process.env.CI ? 2 : 4,
  retries: 0,
  forbidOnly: Boolean(process.env.CI),
  timeout: 45_000,
  expect: { timeout: 8_000 },
  reporter: [['list']],
  use: { trace: 'off', video: 'off', screenshot: 'off', browserName: 'chromium', timezoneId: 'Asia/Taipei' },
});
