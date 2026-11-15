// Records and Permissions (ux sections 4.4, 4.6) in a real browser against the fake
// home: both layouts and both languages with axe, then the flows: a record opened, the
// filters, "Check my records", export, a chain that does not check out, Stop with a
// place that fails once and then turns off, and the link from a permission to its records.
import { mkdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import type { Browser, Locator, Page } from '@playwright/test';
import { axeClean, DESKTOP, expect, noSidewaysScroll, PHONE, test, watch, type Stack } from './stack';


const LAYOUTS = [
  { name: 'phone', viewport: PHONE },
  { name: 'desktop', viewport: DESKTOP },
] as const;
const LANGS = [
  { name: 'zh', locale: 'zh-TW' },
  { name: 'en', locale: 'en-GB' },
] as const;
type Layout = (typeof LAYOUTS)[number];
type Lang = (typeof LANGS)[number];
const shots = process.env.MUSE_SHOTS_DIR;

async function newPage(browser: Browser, stack: Stack, layout: Layout, lang: Lang, scheme: 'light' | 'dark' = 'light') {
  const context = await browser.newContext({ viewport: layout.viewport, locale: lang.locale, colorScheme: scheme, deviceScaleFactor: 2, baseURL: stack.url, acceptDownloads: true });
  const page = await context.newPage();
  return { context, page, w: watch(page, stack.url) };
}

async function shoot(page: Page, name: string) {
  if (!shots) return;
  mkdirSync(shots, { recursive: true });
  const extra = await page.evaluate(() => {
    const el = document.querySelector('.scroll');
    return el ? el.scrollHeight - el.clientHeight : 0;
  });
  const vp = page.viewportSize();
  if (vp && extra > 0) await page.setViewportSize({ width: vp.width, height: vp.height + extra });
  await page.screenshot({ path: join(shots, `${name}.png`), fullPage: true });
  if (vp) await page.setViewportSize(vp);
}

async function check(page: Page, w: ReturnType<typeof watch>) {
  await axeClean(page);
  await noSidewaysScroll(page);
  expect(w.problems).toEqual([]);
}

async function permCard(page: Page, text: RegExp): Promise<Locator> {
  const card = page.getByTestId('perm').filter({ has: page.getByRole('heading', { level: 3, name: text }) });
  await expect(card).toHaveCount(1);
  return card;
}

// ---- both screens, both layouts, both languages

for (const layout of LAYOUTS) {
  for (const lang of LANGS) {
    test(`/records ${layout.name} ${lang.name}: every record as a sentence, accessible, stays in the app`, async ({ browser, shared }) => {
      const stack = await shared('normal');
      const { context, page, w } = await newPage(browser, stack, layout, lang);
      await page.goto('/records');
      await expect(page.getByTestId('screen-records')).toHaveAttribute('data-loaded', 'true');
      await expect(page.getByTestId('records-ok')).toBeVisible();
      expect(await page.getByTestId('record-row').count()).toBeGreaterThan(10);
      await expect(page.getByTestId('record-row').filter({ hasText: lang.name === 'en' ? 'What it said was removed' : '內容已移除' }).first()).toBeAttached();
      await expect(page.locator('main')).not.toContainText(/llm\.call|mandate|m1_|summary_/);
      await check(page, w);
      await shoot(page, `records-${layout.name}-${lang.name}`);
      const row = page.getByTestId('record-row').nth(3);
      await (layout.name === 'desktop' ? row.getByRole('button') : row).click();
      await expect(page.getByTestId('record-detail')).toBeVisible();
      await expect(page.getByTestId('record-proof')).toBeVisible();
      await check(page, w);
      await shoot(page, `records-${layout.name}-${lang.name}-record`);
      await context.close();
    });

    test(`/permissions ${layout.name} ${lang.name}: sentences, marks, declined; accessible`, async ({ browser, shared }) => {
      const stack = await shared('normal');
      const { context, page, w } = await newPage(browser, stack, layout, lang);
      await page.goto('/permissions');
      await expect(page.getByTestId('screen-permissions')).toHaveAttribute('data-loaded', 'true');
      await expect(page.getByTestId('perm-list').getByTestId('perm')).toHaveCount(3);
      await expect(page.getByTestId('perm-declined')).toContainText('openmausbot-tools');
      await page.getByTestId('perm-ended-toggle').click();
      await expect(page.getByTestId('perm-ended').getByTestId('perm')).toHaveCount(2);
      await expect(page.locator('main')).not.toContainText(/mandate|token|m1_/i);
      await check(page, w);
      await shoot(page, `permissions-${layout.name}-${lang.name}`);
      await context.close();
    });
  }
}

test('dark: both screens stay readable', async ({ browser, shared }) => {
  const stack = await shared('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1], 'dark');
  for (const path of ['/records', '/permissions']) {
    await page.goto(path);
    await expect(page.getByTestId(`screen-${path.slice(1)}`)).toHaveAttribute('data-loaded', 'true');
    await page.waitForTimeout(200);
    await check(page, w);
    await shoot(page, `${path.slice(1)}-desktop-dark`);
  }
  await context.close();
});

// ---- Records

test('records: filters, Check my records, and export of a file that checks', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/records');
  await expect(page.getByTestId('records-ok')).toBeVisible();
  const all = await page.getByTestId('record-row').count();
  await page.getByTestId('chip-cost').click();
  await expect(page.getByTestId('record-row')).toHaveCount(1);
  await expect(page.getByTestId('record-row')).toContainText(/You asked DeepSeek\./i);
  await expect(page.getByTestId('record-row')).toContainText('about NT$1.3');
  await page.getByTestId('chip-cost').click();
  await page.getByTestId('chip-left').click();
  expect(await page.getByTestId('record-row').count()).toBeLessThan(all);
  await page.getByTestId('chip-left').click();
  await page.getByTestId('filter-when').selectOption('today');
  expect(await page.getByTestId('record-row').count()).toBeLessThan(all);
  await page.getByTestId('filter-when').selectOption('all');
  await page.getByTestId('records-check').click();
  await expect(page.getByTestId('records-check-note')).toHaveText(/^Checked just now: all \d+ records check out\.$/);
  // Export: the home seals, checks and hands over the file; the app saves it.
  await page.getByTestId('records-export').click();
  await page.getByTestId('export-redact').check();
  const [download] = await Promise.all([page.waitForEvent('download'), page.getByTestId('export-go').click()]);
  expect(download.suggestedFilename()).toMatch(/^muse-records-\d{4}-\d{2}-\d{2}\.json$/);
  const file = JSON.parse(readFileSync(await download.path(), 'utf8')) as { type: string; complete: boolean; records: { summary?: string }[] };
  expect(file.type).toBe('muse.records.export');
  expect(file.complete).toBe(true);
  expect(file.records.every((r) => r.summary === undefined)).toBe(true);
  await expect(page.getByTestId('export-note')).toContainText('without what they say');
  await check(page, w);
  await shoot(page, 'records-desktop-en-export');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('records-export')).toBeFocused();
  // The export itself is now a record.
  const after = await stack.home('GET', '/v1/receipts?limit=1');
  expect(after.body.records[0].action).toBe('records.export');
  await context.close();
});

test('records that do not check out: which one, and nothing is exported', async ({ browser, shared }) => {
  const stack = await shared('mismatch');
  for (const layout of LAYOUTS) {
    for (const lang of LANGS) {
      const { context, page, w } = await newPage(browser, stack, layout, lang);
      await page.goto('/records');
      const bad = page.getByTestId('records-bad');
      await expect(bad).toContainText(lang.name === 'en' ? /Record \d+ does not match the ones around it\./ : /第 \d+ 筆紀錄和前後的對不起來。/);
      await bad.getByRole('button').click();
      await expect(page.getByTestId('record-row').filter({ hasText: lang.name === 'en' ? 'Does not match' : '對不起來' })).toHaveCount(1);
      await check(page, w);
      await shoot(page, `records-${layout.name}-${lang.name}-mismatch`);
      if (layout.name === 'desktop' && lang.name === 'en') {
        await page.getByTestId('records-check').click();
        await expect(page.getByTestId('records-check-note')).toHaveText(/^Checked just now: record \d+ does not match\.$/);
        await page.getByTestId('records-export').click();
        await page.getByTestId('export-go').click();
        await expect(page.getByTestId('export-note')).toHaveText('Your records do not check out, so nothing was exported.');
      }
      await context.close();
    }
  }
});

for (const state of ['empty', 'error', 'paused'] as const) {
  test(`records and permissions in the ${state} state`, async ({ browser, shared }) => {
    const stack = await shared(state);
    const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
    if (state === 'error') w.allowHomeFailures();
    await page.goto('/records');
    if (state === 'empty') await expect(page.getByTestId('records-empty')).toBeVisible();
    if (state === 'error') await expect(page.getByTestId('records-problem')).toBeVisible();
    if (state === 'paused') await expect(page.getByTestId('records-paused')).toBeVisible();
    await page.waitForTimeout(300);
    await check(page, w);
    await shoot(page, `records-phone-en-${state}`);
    await page.goto('/permissions');
    if (state === 'empty') await expect(page.getByTestId('perms-empty')).toBeVisible();
    if (state === 'error') await expect(page.getByTestId('perms-problem')).toBeVisible();
    if (state === 'paused') await expect(page.getByTestId('perm-list')).toBeVisible();
    await page.waitForTimeout(300);
    await check(page, w);
    await shoot(page, `permissions-phone-en-${state}`);
    await context.close();
  });
}

// ---- Permissions: Stop

test('stop: two taps; each place turns off one by one; a place that fails is shown, and Try again finishes it', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  await page.goto('/permissions');
  const card = await permCard(page, /coding-tool/);
  const name = await card.getAttribute('data-perm');
  await card.getByTestId('perm-stop').click(); // tap 1
  const dialog = page.getByTestId('stop-dialog');
  await expect(dialog).toContainText('This program won’t be able to use your allowances any more.');
  await check(page, w);
  await shoot(page, 'permissions-phone-en-stop-ask');
  await dialog.getByTestId('stop-confirm').click(); // tap 2
  const router = dialog.locator('li[data-target="router"]');
  const records = dialog.locator('li[data-target="records"]');
  await expect(router).toHaveAttribute('data-state', 'failed'); // the fake home's router entry fails once, on purpose
  await expect(records).toHaveAttribute('data-state', 'waiting'); // shown one by one
  await expect(records).toHaveAttribute('data-state', 'revoked');
  await expect(dialog.getByTestId('stop-partial')).toContainText('Stopped, but one part could not be turned off yet.');
  await expect(router).toContainText('Using your allowances: Couldn’t be turned off yet');
  await check(page, w);
  await shoot(page, 'permissions-phone-en-stop-failed');
  await dialog.getByTestId('stop-retry').click();
  await expect(dialog.getByTestId('stop-done')).toHaveText('Stopped. A new line is in Records.');
  await expect(router).toHaveAttribute('data-state', 'revoked');
  await shoot(page, 'permissions-phone-en-stop-done');
  await dialog.getByTestId('stop-close').click();
  // Focus goes to the stopped permission, now under "Stopped or ended".
  const stopped = page.getByTestId('perm-ended').locator(`[data-perm="${name}"]`);
  await expect(stopped).toHaveAttribute('data-state', 'stopped');
  await expect(stopped.getByRole('heading', { level: 3 })).toBeFocused();
  await expect(stopped.getByTestId('perm-marks')).toContainText('off');
  // On the home itself.
  const listed = (await stack.home('GET', '/v1/mandates')).body.mandates as { name: string; state: string; targets: Record<string, { state: string }> }[];
  const m = listed.find((x) => x.name === name);
  expect(m?.state).toBe('stopped');
  expect(Object.values(m?.targets ?? {}).every((t) => t.state === 'revoked')).toBe(true);
  // Records says so.
  await page.getByRole('link', { name: 'Records', exact: true }).click();
  await expect(page.getByTestId('record-row').first()).toContainText('You stopped a permission.');
  expect(w.problems).toEqual([]);
  await context.close();
});

test('stop on a desktop in zh-TW: the spending limit honestly says there was nothing to turn off', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[0]);
  await page.goto('/permissions');
  const card = await permCard(page, /NT\$30/);
  await expect(card.getByTestId('perm-marks')).toContainText('還沒做好');
  await card.getByTestId('perm-stop').click();
  const dialog = page.getByTestId('stop-dialog');
  await expect(dialog).toContainText('你的小幫手將不能在這些服務上花錢。');
  await dialog.getByTestId('stop-confirm').click();
  await expect(dialog.getByTestId('stop-done')).toHaveText('已停止。「紀錄」裡多了一筆。');
  await expect(dialog.locator('li[data-target="payments"]')).toContainText('沒有東西要關：花費上限還沒做好');
  await check(page, w);
  await shoot(page, 'permissions-desktop-zh-stop-done');
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('perm-ended')).toBeVisible();
  await context.close();
});

test('a permission links to its records, filtered, and back to all', async ({ browser, shared }) => {
  const stack = await shared('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/permissions');
  const card = await permCard(page, /coding-tool/);
  await expect(card).toContainText('Used once');
  await card.getByTestId('perm-records').click();
  await expect(page).toHaveURL(/\/records\?permission=m1_[0-9a-f]{64}$/);
  await expect(page.getByTestId('records-under')).toContainText('Only records under: The program “coding-tool” may use your allowances');
  await expect(page.getByTestId('record-row')).toHaveCount(2); // given, then used once
  await check(page, w);
  await shoot(page, 'records-desktop-en-permission');
  await page.getByRole('button', { name: 'Show all records' }).click();
  await expect(page).toHaveURL(/\/records$/);
  await expect.poll(() => page.getByTestId('record-row').count()).toBeGreaterThan(10);
  await page.goBack();
  await expect(page).toHaveURL(/\/permissions$/);
  await context.close();
});

test('keyboard: the stop dialog can be used and left without a mouse; focus comes back to Stop', async ({ browser, shared }) => {
  const stack = await shared('normal');
  const { context, page } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/permissions');
  const card = await permCard(page, /run tasks for you/);
  await card.getByTestId('perm-stop').focus();
  await page.keyboard.press('Enter');
  await expect(page.getByTestId('stop-dialog')).toBeVisible();
  await expect(page.getByTestId('stop-confirm')).toBeFocused();
  await page.keyboard.press('Escape');
  await expect(page.getByTestId('stop-dialog')).toBeHidden();
  await expect(card.getByTestId('perm-stop')).toBeFocused();
  // Nothing was stopped.
  await expect(card).toHaveAttribute('data-state', 'active');
  await context.close();
});

test('phone: a record opens in a sheet; closing it returns focus to the line', async ({ browser, shared }) => {
  const stack = await shared('normal');
  const { context, page } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  await page.goto('/records');
  const row = page.getByTestId('record-row').nth(1);
  await row.click();
  await expect(page.getByTestId('record-sheet')).toBeVisible();
  await page.getByTestId('record-close').click();
  await expect(row).toBeFocused();
  await context.close();
});
