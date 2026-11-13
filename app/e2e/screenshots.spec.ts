// Not a test of behaviour: takes the pictures a person looks at to check the layout.
//   MUSE_SHOTS_DIR=/some/folder pnpm e2e screenshots
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { DESKTOP, PHONE, STATES, test, expect, type FakeState } from './stack';

const dir = process.env.MUSE_SHOTS_DIR;

const VARIANTS = [
  { name: 'phone-zh', viewport: PHONE, locale: 'zh-TW', colorScheme: 'light' },
  { name: 'phone-en', viewport: PHONE, locale: 'en-GB', colorScheme: 'light' },
  { name: 'desktop-zh', viewport: DESKTOP, locale: 'zh-TW', colorScheme: 'light' },
  { name: 'desktop-dark', viewport: DESKTOP, locale: 'en-GB', colorScheme: 'dark' },
] as const;

test.describe('screenshots', () => {
  test.skip(!dir, 'set MUSE_SHOTS_DIR to take screenshots');

  for (const v of VARIANTS) {
    for (const state of [...STATES, 'loading', 'unreachable'] as const) {
      test(`${v.name} ${state}`, async ({ browser, shared, fresh }) => {
        mkdirSync(dir as string, { recursive: true });
        const special = state === 'loading' || state === 'unreachable';
        const stack = state === 'unreachable' ? await fresh('normal') : await shared(special ? 'normal' : (state as FakeState));
        const context = await browser.newContext({ viewport: v.viewport, locale: v.locale, colorScheme: v.colorScheme, deviceScaleFactor: 2, baseURL: stack.url });
        const page = await context.newPage();
        if (state === 'loading') {
          await page.route('**/v1/status', async (route) => {
            await new Promise((r) => setTimeout(r, 4000));
            await route.continue().catch(() => undefined);
          });
        }
        await page.goto('/today');
        if (state === 'loading') await expect(page.getByTestId('updating')).toBeVisible();
        else if (state === 'error') await expect(page.getByTestId('unreachable')).toBeVisible();
        else await expect(page.getByTestId('today')).toHaveAttribute('data-loaded', 'true');
        if (state === 'unreachable') {
          await stack.stopHome();
          await page.getByTestId('pause').focus();
          await expect(page.getByTestId('unreachable')).toBeVisible({ timeout: 20_000 });
        }
        // The page scrolls inside its frame, so make the window as tall as the content to show all of it.
        const extra = await page.evaluate(() => {
          const el = document.querySelector('.scroll');
          return el ? el.scrollHeight - el.clientHeight : 0;
        });
        if (extra > 0) await page.setViewportSize({ width: v.viewport.width, height: v.viewport.height + extra });
        await page.screenshot({ path: join(dir as string, `today-${v.name}-${state}.png`), fullPage: true });
        await page.setViewportSize(v.viewport);
        if (state === 'normal') {
          await page.goto('/tray');
          await expect(page.getByTestId('tray-gauge')).toBeVisible();
          await page.screenshot({ path: join(dir as string, `tray-${v.name}.png`), fullPage: true });
          await page.goto('/settings');
          await page.screenshot({ path: join(dir as string, `settings-${v.name}.png`), fullPage: true });
          await page.goto(v.viewport === PHONE ? '/more' : '/records');
          await page.screenshot({ path: join(dir as string, `${v.viewport === PHONE ? 'more' : 'records'}-${v.name}.png`), fullPage: true });
        }
        await context.close();
      });
    }
  }
});
