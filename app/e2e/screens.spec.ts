// The app's screens (Ask, Chores, Services, Settings, For apps, first run) in a real
// browser against the fake home: every screen in both layouts and both languages,
// with axe, no console errors, no request leaving the app and no sideways scroll;
// then the flows a person goes through.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import type { Browser, Page } from '@playwright/test';
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

/** A page whose window.open only records what it was asked to open: no test may reach a real service. */
async function newPage(browser: Browser, stack: Stack, layout: Layout, lang: Lang, scheme: 'light' | 'dark' = 'light') {
  const context = await browser.newContext({ viewport: layout.viewport, locale: lang.locale, colorScheme: scheme, deviceScaleFactor: 2, baseURL: stack.url });
  await context.addInitScript(() => {
    const w = window as unknown as { __opened: string[] };
    w.__opened = [];
    window.open = ((url?: string | URL) => {
      w.__opened.push(String(url));
      return null;
    }) as typeof window.open;
  });
  const page = await context.newPage();
  const w = watch(page, stack.url);
  return { context, page, w };
}

async function opened(page: Page): Promise<string[]> {
  return page.evaluate(() => (window as unknown as { __opened: string[] }).__opened);
}

async function shoot(page: Page, name: string) {
  if (!shots) return;
  mkdirSync(shots, { recursive: true });
  const extra = await page.evaluate(() => {
    const el = document.querySelector('.scroll') ?? document.querySelector('.firstrun');
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

// ---- every screen, both layouts, both languages

const SCREENS = [
  { path: '/ask', id: 'screen-ask', ready: 'ask-form' },
  { path: '/chores', id: 'screen-chores', ready: 'chore-list' },
  { path: '/services', id: 'screen-services', ready: 'service-list' },
  { path: '/settings', id: 'screen-settings', ready: 'limit-form' },
  { path: '/apps', id: 'screen-apps', ready: 'endpoint' },
] as const;

for (const layout of LAYOUTS) {
  for (const lang of LANGS) {
    for (const s of SCREENS) {
      if (s.path === '/apps' && layout.name === 'phone') continue; // For apps is desktop only
      test(`${s.path} ${layout.name} ${lang.name}: shows, is accessible, stays in the app`, async ({ browser, shared }) => {
        const stack = await shared('normal');
        const { context, page, w } = await newPage(browser, stack, layout, lang);
        await page.goto(s.path);
        await expect(page.getByTestId(s.id)).toBeVisible();
        await expect(page.getByTestId(s.ready)).toBeVisible();
        await check(page, w);
        await shoot(page, `${s.path.slice(1)}-${layout.name}-${lang.name}`);
        await context.close();
      });
    }
  }
}

for (const state of ['used', 'empty', 'error', 'paused'] as const) {
  for (const s of SCREENS.slice(0, 3)) {
    test(`${s.path} in the ${state} state`, async ({ browser, shared }) => {
      const stack = await shared(state);
      const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
      if (state === 'error') w.allowHomeFailures();
      await page.goto(s.path);
      await expect(page.getByTestId(s.id)).toBeVisible();
      if (state === 'error' && s.path !== '/ask') await expect(page.getByTestId(`${s.path.slice(1)}-problem`)).toBeVisible();
      if (state === 'empty' && s.path === '/services') await expect(page.getByTestId('services-empty')).toBeVisible();
      if (state === 'paused' && s.path === '/ask') await expect(page.getByTestId('ask-send')).toBeDisabled();
      await page.waitForTimeout(300);
      await check(page, w);
      await shoot(page, `${s.path.slice(1)}-phone-en-${state}`);
      await context.close();
    });
  }
}

// ---- flows

async function ask(page: Page, text: string) {
  await page.getByTestId('ask-input').fill(text);
  await page.getByTestId('ask-send').click();
}

test('ask → answer with the grey line, Details, and Make this a chore', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  for (const layout of LAYOUTS) {
    const { context, page, w } = await newPage(browser, stack, layout, LANGS[1]);
    await page.goto('/ask');
    await ask(page, 'What is a good lunch?');
    const turn = page.getByTestId('turn').last();
    await expect(turn).toHaveAttribute('data-status', 'answered');
    await expect(turn.getByTestId('answer-text')).toContainText('canned answer');
    await expect(turn.getByTestId('grey-line')).toContainText(/^Used your Groq allowance.*Nothing spent\./);
    await turn.getByTestId('grey-line').click();
    await expect(page.getByTestId('answer-details')).toContainText('groq-gpt-oss-20b');
    await expect(page.getByTestId('left-device')).toContainText('Deleting this conversation here does not delete anything Groq kept');
    await check(page, w);
    await shoot(page, `ask-answer-${layout.name}-en`);
    await turn.getByTestId('make-chore').click();
    await expect(page.getByTestId('chore-sentence')).toHaveValue('What is a good lunch?');
    await context.close();
  }
});

test('ask in Chinese: the answer, its grey line and the files path with sources', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[0]);
  await page.goto('/ask');
  await page.getByTestId('attach').locator('summary').click();
  await page.getByRole('checkbox', { name: 'documents' }).check();
  await page.getByTestId('attach').locator('summary').click();
  await ask(page, '租金什麼時候付？');
  const turn = page.getByTestId('turn').last();
  await expect(turn).toHaveAttribute('data-status', 'answered');
  await expect(turn.getByTestId('sources')).toContainText('documents/Lease 2026.md › 第 4 段');
  await expect(turn.getByTestId('sources').locator('blockquote')).toContainText('租金每月');
  await expect(turn.getByTestId('grey-line')).toHaveText(/由 Groq 回答。沒有花錢。/);
  await check(page, w);
  await shoot(page, 'ask-files-phone-zh');
  await context.close();
});

test('a cut-short answer: Continue asks again with more room; Markdown shows lists and a table', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/ask');
  await ask(page, 'Write a long plan for the rent');
  const turn = page.getByTestId('turn').last();
  await expect(turn.getByTestId('cut-short')).toContainText('The answer was cut short.');
  await turn.getByTestId('continue').click();
  await expect(turn.getByTestId('cut-short')).toHaveCount(0);
  await expect(turn.locator('.md table')).toBeVisible();
  await expect(turn.locator('.md ol li')).toHaveCount(3);
  await check(page, w);
  await shoot(page, 'ask-markdown-desktop-en');
  await context.close();
});

test('ask → the three choices in their order → wait, open in my app, spend, and over the limit', async ({ browser, fresh }) => {
  const stack = await fresh('used');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  await page.goto('/ask');
  await ask(page, 'hello');
  let turn = page.getByTestId('turn').last();
  const buttons = turn.getByTestId('ask-choices').getByRole('button');
  await expect(buttons).toHaveCount(3);
  expect(await buttons.evaluateAll((bs) => bs.map((b) => b.getAttribute('data-choice')))).toEqual(['wait', 'handoff', 'pay']);
  await expect(buttons.nth(0)).toHaveText(/^Wait until \d\d:\d\d$/);
  await expect(buttons.nth(1)).toHaveText('Open in my ChatGPT');
  await expect(buttons.nth(2)).toHaveText('Spend about NT$1');
  await check(page, w);
  await shoot(page, 'ask-choices-phone-en');
  await buttons.nth(0).click();
  await expect(turn.getByTestId('queued')).toContainText(/Will be asked at \d\d:\d\d/);

  await ask(page, 'hello again');
  turn = page.getByTestId('turn').last();
  await turn.locator('[data-choice=handoff]').click();
  await expect(turn.getByTestId('handed-off')).toContainText('Opened your ChatGPT. Nothing spent.');
  await expect.poll(async () => (await opened(page)).some((u) => u.startsWith('https://chatgpt.com/'))).toBe(true);

  await ask(page, 'one more');
  turn = page.getByTestId('turn').last();
  await turn.locator('[data-choice=pay]').click();
  await expect(turn).toHaveAttribute('data-status', 'answered');
  await expect(turn.getByTestId('grey-line')).toHaveText(/Answered by DeepSeek\. Spent about NT\$1\./);

  await ask(page, 'x'.repeat(220));
  turn = page.getByTestId('turn').last();
  await turn.locator('[data-choice=pay]').click();
  await expect(turn.getByTestId('over-limit')).toContainText('over today’s limit');
  await turn.getByTestId('raise-today').click();
  await expect(turn).toHaveAttribute('data-status', 'answered');
  expect(w.problems).toEqual([]);
  await context.close();
});

test('private with no private service says so, with the way out', async ({ browser, fresh }) => {
  const stack = await fresh('used');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/ask');
  await page.getByTestId('privacy').selectOption('private');
  await ask(page, 'my health question');
  await expect(page.getByTestId('ask-failed')).toContainText('No connected service keeps this private.');
  await check(page, w);
  await context.close();
});

test('offline: the question waits and is asked when the home is back', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  w.allowHomeFailures();
  await page.goto('/ask');
  await expect(page.getByTestId('ask-form')).toBeVisible();
  await page.route('**/v1/**', (route) => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":{"code":"unavailable","message":"x"}}' }));
  await ask(page, 'are you there?');
  await expect(page.getByTestId('ask-offline')).toContainText('You’re offline.');
  await page.unroute('**/v1/**');
  await expect(page.getByTestId('turn').last()).toHaveAttribute('data-status', 'answered', { timeout: 20_000 });
  await context.close();
});

test('guided key → connected (the key goes in a password field and is cleared)', async ({ browser, fresh }) => {
  const stack = await fresh('empty');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/services');
  await page.locator('[data-testid=add-service][data-service=cerebras]').click();
  await expect(page.getByTestId('guide')).toContainText('Cerebras');
  await page.getByTestId('guide-open').click();
  await expect.poll(async () => (await opened(page)).some((u) => u.startsWith('https://cloud.cerebras.ai'))).toBe(true);
  while ((await page.getByTestId('key-form').count()) === 0) await page.getByTestId('guide-next').click();
  await shoot(page, 'services-guide-desktop-en');
  const input = page.getByTestId('key-input');
  await expect(input).toHaveAttribute('type', 'password');
  await input.fill('csk-fake0000000000000000000000000000000000');
  await page.getByTestId('key-submit').click();
  await expect(page.getByTestId('connected-note')).toContainText('Cerebras connected.');
  await expect(page.locator('[data-testid=service][data-service=cerebras]')).toBeVisible();
  expect(await page.content()).not.toContain('csk-fake');
  await check(page, w);
  await context.close();
});

test('services: test it and disconnect', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  await page.goto('/services');
  const groq = page.locator('[data-testid=service][data-service=groq]');
  await groq.getByRole('button', { name: 'Details' }).click();
  await groq.getByTestId('service-test').click();
  await expect(groq.getByTestId('service-test-result')).toHaveText('It works.');
  await groq.getByRole('button', { name: 'Disconnect' }).click();
  await groq.getByTestId('service-disconnect').click();
  await expect(page.getByTestId('connected-note')).toContainText('Groq disconnected.');
  await expect(groq).toHaveCount(0);
  expect(w.problems).toEqual([]);
  await context.close();
});

test('add a chore by a sentence → it appears → run now → its card waits on Today', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  for (const lang of LANGS) {
    const { context, page, w } = await newPage(browser, stack, LAYOUTS[0], lang);
    await page.goto('/chores');
    await expect(page.getByTestId('chore-list')).toBeVisible();
    const before = await page.getByTestId('chore').count();
    await page.getByTestId('chore-add-btn').click();
    await page.getByTestId('chore-sentence').fill('summarise stuff');
    await page.getByTestId('chore-understand').click();
    await expect(page.getByTestId('chore-questions')).toBeVisible();
    await page.getByRole('button', { name: lang.name === 'en' ? 'Change the sentence' : '修改這句話' }).click();
    await page.getByTestId('chore-sentence').fill('Every day at 07:30, write me a daily brief');
    await page.getByTestId('chore-understand').click();
    await expect(page.getByTestId('chore-understood')).toBeVisible();
    await shoot(page, `chores-understood-phone-${lang.name}`);
    await page.getByTestId('chore-confirm').click();
    await expect(page.getByTestId('chore')).toHaveCount(before + 1);
    const made = page.locator('.chore.sel');
    await made.getByTestId('chore-run').click();
    await expect(made.getByTestId('chore-note')).not.toBeEmpty();
    await expect(made.getByTestId('chore-history')).toBeVisible();
    await check(page, w);
    await context.close();
  }
  const cards = await stack.home('GET', '/v1/cards?status=pending');
  expect(cards.body.cards.some((c: { title_key: string | null }) => c.title_key === 'save_file_chore')).toBe(true);
});

test('change the daily limit; Today shows it in the person’s currency', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await page.goto('/settings');
  await expect(page.getByTestId('limit-input')).toHaveValue('30');
  await page.getByTestId('limit-input').fill('60');
  await expect(page.getByTestId('limit-usd')).toHaveText('About US$1.88');
  await page.getByTestId('limit-save').click();
  await expect(page.getByTestId('limit-note')).toHaveText('Saved.');
  expect((await stack.home('GET', '/v1/settings')).body.settings.daily_limit.local).toBe('60');
  await page.getByTestId('verify').click();
  await expect(page.getByTestId('verify-result')).toContainText('This installation checks out.');
  await check(page, w);
  await shoot(page, 'settings-desktop-en');
  await page.goto('/today');
  await expect(page.getByTestId('spending')).toContainText('of NT$60');
  await context.close();
});

test('first run with the one-tap flow: welcome → connect → try it → the answer with its grey line', async ({ browser, fresh }) => {
  const stack = await fresh('empty');
  let first = true;
  for (const lang of LANGS) {
    for (const layout of LAYOUTS) {
      const { context, page, w } = await newPage(browser, stack, layout, lang);
      if (first) {
        // A fresh home with nothing connected: opening the app goes straight into the first run.
        await page.goto('/today');
        await expect(page).toHaveURL(/\/welcome$/);
        first = false;
      } else {
        await page.goto('/welcome');
      }
      await expect(page.getByTestId('first-run')).toHaveAttribute('data-step', 'welcome');
      await check(page, w);
      await shoot(page, `firstrun-welcome-${layout.name}-${lang.name}`);
      await page.getByTestId('fr-continue').click();
      await expect(page.getByTestId('first-run')).toHaveAttribute('data-step', /service|try/);
      if ((await page.getByTestId('first-run').getAttribute('data-step')) === 'service') {
        await expect(page.getByTestId('one-tap-facts')).toContainText(lang.name === 'en' ? '50 requests a day for free.' : '每天免費 50 次。');
        await shoot(page, `firstrun-service-${layout.name}-${lang.name}`);
        await page.getByTestId('one-tap-start').click();
        await expect.poll(async () => (await opened(page)).some((u) => u.startsWith('https://openrouter.ai/'))).toBe(true);
      }
      await expect(page.getByTestId('first-run')).toHaveAttribute('data-step', 'try', { timeout: 10_000 });
      await page.getByTestId('fr-input').fill(lang.name === 'en' ? 'What can you do?' : '你能做什麼？');
      await page.getByTestId('fr-ask').click();
      await expect(page.getByTestId('first-run')).toHaveAttribute('data-step', 'answer');
      await expect(page.getByTestId('grey-line')).toHaveText(lang.name === 'en' ? 'Answered by OpenRouter. Nothing spent.' : '由 OpenRouter 回答。沒有花錢。');
      await expect(page.getByText(/recovery|救援/)).toHaveCount(0); // no made-up recovery words: the vault is not wired in
      await check(page, w);
      await shoot(page, `firstrun-answer-${layout.name}-${lang.name}`);
      await page.getByTestId('fr-done').click();
      await expect(page.getByTestId('today')).toBeVisible();
      await context.close();
    }
  }
  expect((await stack.home('GET', '/v1/settings')).body.settings.first_run).toBe('done');
});

test('first run opens by itself once; "Skip for now" is remembered on the home, and Today keeps its empty state', async ({ browser, fresh }) => {
  const stack = await fresh('empty');
  expect((await stack.home('GET', '/v1/settings')).body.settings.first_run).toBe('pending');
  const one = await newPage(browser, stack, LAYOUTS[0], LANGS[1]);
  await one.page.goto('/today');
  await expect(one.page).toHaveURL(/\/welcome$/);
  await expect(one.page.getByTestId('first-run')).toHaveAttribute('data-step', 'welcome');
  await one.page.getByTestId('fr-skip').click();
  await expect(one.page.getByTestId('nothing-connected')).toBeVisible();
  await expect.poll(async () => (await stack.home('GET', '/v1/settings')).body.settings.first_run).toBe('skipped');
  await check(one.page, one.w);
  await one.context.close();
  // A new browser (nothing in its storage) on the same home: Today, not the first run.
  const two = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await two.page.goto('/today');
  await expect(two.page.getByTestId('nothing-connected')).toBeVisible();
  await two.page.waitForTimeout(500);
  await expect(two.page).toHaveURL(/\/today$/);
  await expect(two.page.getByTestId('first-run')).toHaveCount(0);
  await check(two.page, two.w);
  await two.context.close();
});

test('For apps: the address, a new program, its key copied and never shown', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const { context, page, w } = await newPage(browser, stack, LAYOUTS[1], LANGS[1]);
  await context.grantPermissions(['clipboard-read', 'clipboard-write']);
  await page.goto('/apps');
  await expect(page.getByTestId('endpoint')).toHaveText('http://127.0.0.1:4000/v1');
  await page.getByRole('radio', { name: 'Open WebUI' }).check();
  await page.getByTestId('program-name').fill('my-chat');
  await page.getByTestId('program-add').click();
  await expect(page.getByTestId('program-made')).toContainText('my-chat added.');
  await page.getByTestId('copy-key').click();
  await expect(page.getByTestId('program-made')).toContainText('Key copied.');
  const copiedKey = await page.evaluate(() => navigator.clipboard.readText());
  expect(copiedKey.length).toBeGreaterThan(16);
  expect(await page.content()).not.toContain(copiedKey);
  await expect(page.getByTestId('programs')).toContainText('my-chat');
  await check(page, w);
  await shoot(page, 'apps-made-desktop-en');
  await context.close();
});

test('cards the home made itself are shown in the person’s language', async ({ browser, fresh }) => {
  const stack = await fresh('normal');
  const chores = await stack.home('GET', '/v1/chores');
  await stack.home('POST', `/v1/chores/${chores.body.chores[0].id}/run`);
  const { context, page } = await newPage(browser, stack, LAYOUTS[0], LANGS[0]);
  await page.goto('/today');
  const card = page.locator('[data-testid=card][data-kind=save_file]').first();
  await expect(card.locator('h3')).toHaveText('儲存檔案', { timeout: 15_000 });
  await expect(card).toContainText('例行工作「Weekly digest」') // the label is the chore's own words;
  await context.close();
});
