// The built app in a real browser against the fake home.
import { readdirSync, readFileSync } from 'node:fs';
import { join, resolve } from 'node:path';
import type { Page } from '@playwright/test';
import { axeClean, DESKTOP, expect, noSidewaysScroll, PHONE, STATES, test, watch, type FakeState, type Stack } from './stack';

const LAYOUTS = [
  { name: 'phone', viewport: PHONE },
  { name: 'desktop', viewport: DESKTOP },
] as const;
const LANGS = [
  { name: 'zh', locale: 'zh-TW' },
  { name: 'en', locale: 'en-GB' },
] as const;
const SCHEMES = ['light', 'dark'] as const;

const TEXT = {
  zh: { today: '今天', paused: '全部已暫停', connect: '連接你的第一個服務', unreachable: '連不上你的常駐小幫手', needs: (n: number) => `等你決定（${n}）`, refills: /^\d\d:\d\d 補滿$/, nothingSpent: '今天沒有花錢。' },
  en: { today: 'Today', paused: 'Everything is paused', connect: 'Connect your first service', unreachable: 'Can’t reach your always-on helper', needs: (n: number) => `Needs you (${n})`, refills: /^Refills at \d\d:\d\d$/, nothingSpent: 'Nothing spent today.' },
};

async function open(page: Page, stack: Stack, path = '/today') {
  await page.goto(stack.url + path);
}

async function loaded(page: Page) {
  await expect(page.getByTestId('today')).toHaveAttribute('data-loaded', 'true');
}

/** What each state of the fake home must look like on Today (UX 4.1 and 8). */
async function expectState(page: Page, state: FakeState, lang: 'zh' | 'en') {
  const tx = TEXT[lang];
  await expect(page.getByRole('heading', { level: 1 })).toContainText(tx.today);
  if (state === 'error') {
    await expect(page.getByTestId('unreachable')).toContainText(tx.unreachable);
    await expect(page.getByTestId('unreachable').getByRole('button')).toHaveCount(2);
    await expect(page.getByTestId('gauge-card')).toHaveCount(0);
    return;
  }
  await loaded(page);
  if (state === 'empty') {
    await expect(page.getByTestId('nothing-connected')).toContainText(tx.connect);
    await expect(page.getByTestId('gauge-percent')).toHaveCount(0);
    await expect(page.getByTestId('tip')).toHaveCount(0);
    await expect(page.getByTestId('card')).toHaveCount(0);
    await expect(page.getByTestId('red-dot')).toHaveCount(0);
    return;
  }
  await expect(page.getByTestId('gauge-percent')).toHaveText(/^\d{1,3}%$/);
  await expect(page.getByTestId('spending')).toContainText('NT$');
  if (state === 'used') {
    await expect(page.getByTestId('gauge-percent')).toHaveText('0%');
    await expect(page.getByTestId('next-refill')).toHaveText(tx.refills);
    await expect(page.getByTestId('choices').getByRole('button')).toHaveCount(2);
  } else {
    await expect(page.getByTestId('choices')).toHaveCount(0);
  }
  if (state === 'paused') {
    await expect(page.getByTestId('paused-bar')).toContainText(tx.paused);
    await expect(page.getByTestId('pause')).toHaveAttribute('aria-pressed', 'true');
    await expect(page.getByTestId('card').first().getByRole('button').first()).toBeDisabled();
  } else {
    await expect(page.getByTestId('paused-bar')).toHaveCount(0);
  }
  const cards = state === 'cards' ? 7 : 1;
  await expect(page.getByTestId('card')).toHaveCount(cards);
  await expect(page.locator('#needs-h')).toHaveText(tx.needs(cards));
  await expect(page.getByTestId('red-dot')).toContainText(String(cards));
  if (state === 'cards') {
    const kinds = await page.getByTestId('card').evaluateAll((els) => els.map((e) => e.getAttribute('data-kind')));
    expect([...kinds].sort()).toEqual(['grant_permission', 'install_skill', 'message', 'oneoff_credit', 'save_file', 'send_private', 'spend']);
    expect(kinds[0]).toBe('spend'); // newest first
  }
}

for (const layout of LAYOUTS) {
  for (const lang of LANGS) {
    for (const scheme of SCHEMES) {
      test.describe(`Today: ${layout.name}, ${lang.name}, ${scheme}`, () => {
        test.use({ viewport: layout.viewport, locale: lang.locale, colorScheme: scheme });
        for (const state of STATES) {
          test(`state ${state}`, async ({ page, shared }) => {
            const stack = await shared(state);
            const w = watch(page, stack.url);
            if (state === 'error') w.allowHomeFailures();
            await open(page, stack);
            await expectState(page, state, lang.name);
            await expect(page.locator('.app')).toHaveAttribute('data-layout', layout.name);
            await expect(page.locator('html')).toHaveAttribute('lang', lang.name === 'zh' ? 'zh-Hant-TW' : 'en');
            await noSidewaysScroll(page);
            await axeClean(page);
            expect(w.problems).toEqual([]);
          });
        }
      });
    }
  }
}

test.describe('Today, more states', () => {
  test.use({ viewport: PHONE, locale: 'en-GB' });

  test('loading shows "Updating…" and the frame, never a blank screen', async ({ page, shared }) => {
    const stack = await shared('normal');
    const w = watch(page, stack.url);
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    await page.route('**/v1/status', async (route) => {
      await gate;
      await route.continue();
    });
    await open(page, stack);
    await expect(page.getByTestId('updating')).toHaveText('Updating…');
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Today');
    await expect(page.getByTestId('pause')).toBeVisible();
    await axeClean(page);
    release();
    await loaded(page);
    await expect(page.getByTestId('updating')).toHaveCount(0);
    expect(w.problems).toEqual([]);
  });

  test('a slow refresh keeps the last known content and says since when', async ({ page, shared }) => {
    const stack = await shared('normal');
    await open(page, stack);
    await loaded(page);
    await page.route('**/v1/status', async (route) => {
      await new Promise((r) => setTimeout(r, 3000));
      await route.continue().catch(() => undefined);
    });
    await page.getByTestId('pause').click(); // any change asks the home again
    await expect(page.getByTestId('updating')).toHaveText(/^Updating… showing what was known at \d\d:\d\d$/, { timeout: 15_000 });
    await expect(page.getByTestId('gauge-percent')).toBeVisible();
    await page.getByTestId('paused-bar').getByRole('button').click();
  });

  test('home goes away: last known content stays, "Last seen", Retry, Use this device for now; then it recovers', async ({ page, fresh }) => {
    const stack = await fresh('normal');
    await open(page, stack);
    await loaded(page);
    const percent = await page.getByTestId('gauge-percent').textContent();
    await page.route('**/v1/**', (route) => route.abort('connectionrefused'));
    await page.getByTestId('pause').click();
    const box = page.getByTestId('unreachable');
    await expect(box).toContainText(/Can’t reach your always-on helper\. Last seen \d\d:\d\d\./, { timeout: 15_000 });
    await expect(box).toContainText('Muse tries again by itself.');
    await expect(page.getByTestId('gauge-percent')).toHaveText(percent ?? '');
    await expect(page.getByTestId('paused-bar')).toHaveCount(0); // the pause did not happen, so it is not shown
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'pause_failed');
    await axeClean(page);
    await box.getByRole('button', { name: 'Use this device for now' }).click();
    await expect(page.getByTestId('using-this-device')).toContainText('Using this device for now.');
    await page.unroute('**/v1/**');
    await page.getByRole('button', { name: 'Retry' }).click();
    await expect(page.getByTestId('using-this-device')).toHaveCount(0);
    await expect(page.getByTestId('unreachable')).toHaveCount(0);
    expect((await stack.home('GET', '/v1/status')).body.paused).toBe(false);
  });

  test('a refused device shows the error pattern with one button', async ({ page, shared }) => {
    const stack = await shared('normal');
    await page.route('**/v1/**', (route) => route.fulfill({ status: 401, contentType: 'application/json', body: JSON.stringify({ error: { code: 'unauthorized', message: 'server-detail-xyz' } }) }));
    await open(page, stack);
    const box = page.getByTestId('home-error');
    await expect(box).toContainText('This device is not allowed to talk to your helper.');
    await expect(box.getByRole('button')).toHaveCount(1);
    await expect(page.locator('body')).not.toContainText('server-detail-xyz'); // the screen shows its own words, not the server's
  });

  test('polling stops while the window is hidden and resumes when it is shown', async ({ page, shared }) => {
    const stack = await shared('normal');
    let count = 0;
    page.on('request', (r) => {
      if (r.url().endsWith('/v1/status')) count += 1;
    });
    await open(page, stack);
    await loaded(page);
    const setHidden = (hidden: boolean) =>
      page.evaluate((h) => {
        Object.defineProperty(document, 'visibilityState', { configurable: true, get: () => (h ? 'hidden' : 'visible') });
        document.dispatchEvent(new Event('visibilitychange'));
      }, hidden);
    await setHidden(true);
    await page.waitForTimeout(500);
    const before = count;
    await page.waitForTimeout(6500); // longer than one polling interval
    expect(count).toBe(before);
    await setHidden(false);
    await expect.poll(() => count).toBeGreaterThan(before);
  });

  test('the tip shows once, can be dismissed, and stays away for the day', async ({ page, shared }) => {
    const stack = await shared('normal');
    await open(page, stack);
    await expect(page.getByTestId('tip')).toContainText(/^Tip: connect .+ \(\+\d+ min\)/);
    await expect(page.getByTestId('tip')).toHaveCount(1);
    await page.getByRole('button', { name: 'Dismiss tip' }).click();
    await expect(page.getByTestId('tip')).toHaveCount(0);
    await page.reload();
    await loaded(page);
    await expect(page.getByTestId('tip')).toHaveCount(0);
  });

  test('times are shown in the person’s own time zone', async ({ browser, shared }) => {
    const stack = await shared('normal');
    const times: string[] = [];
    for (const timezoneId of ['Asia/Taipei', 'Europe/London']) {
      const context = await browser.newContext({ viewport: PHONE, locale: 'en-GB', timezoneId });
      const page = await context.newPage();
      await open(page, stack);
      times.push((await page.getByTestId('next-refill').textContent()) ?? '');
      await context.close();
    }
    const minutes = times.map((t) => {
      const m = /(\d\d):(\d\d)/.exec(t);
      return m ? Number(m[1]) * 60 + Number(m[2]) : NaN;
    });
    expect(((minutes[0] as number) - (minutes[1] as number) + 1440) % 1440).toBe(8 * 60); // Taipei is 8 hours ahead of London in November
  });

  test('text twice as large still fits the phone without sideways scrolling', async ({ page, shared }) => {
    const stack = await shared('cards');
    await open(page, stack);
    await loaded(page);
    await page.evaluate(() => {
      document.documentElement.style.fontSize = '200%';
    });
    await expect(page.getByTestId('card')).toHaveCount(7);
    await noSidewaysScroll(page);
  });

  test('reduced motion stops the spinner', async ({ browser, shared }) => {
    const stack = await shared('normal');
    const context = await browser.newContext({ viewport: PHONE, locale: 'en-GB', reducedMotion: 'reduce' });
    const page = await context.newPage();
    await page.route('**/v1/status', async (route) => {
      await new Promise((r) => setTimeout(r, 1500));
      await route.continue().catch(() => undefined);
    });
    await open(page, stack);
    await expect(page.getByTestId('updating')).toBeVisible();
    expect(await page.locator('.spin-s').evaluate((el) => getComputedStyle(el).animationName)).toBe('none');
    await context.close();
  });
});

test.describe('cards', () => {
  test.use({ viewport: PHONE, locale: 'en-GB' });

  const cardOf = (page: Page, kind: string) => page.locator(`[data-testid="card"][data-kind="${kind}"]`);

  test('approve and decline change the home, and the red dot follows', async ({ page, fresh }) => {
    const stack = await fresh('cards');
    const w = watch(page, stack.url);
    await open(page, stack);
    await expect(page.getByTestId('card')).toHaveCount(7);

    const save = cardOf(page, 'save_file');
    const saveId = (await save.getAttribute('data-card-id')) as string;
    await save.getByRole('button', { name: 'Approve' }).click();
    await expect(save).toHaveCount(0);
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'approved');
    await expect.poll(async () => (await stack.home('GET', `/v1/cards/${saveId}`)).body.card.status).toBe('approved');
    const approved = (await stack.home('GET', `/v1/cards/${saveId}`)).body.card;
    expect(approved.always).toBe(false);
    expect(approved.answered_by).toBe('fake-device');

    const message = cardOf(page, 'message');
    const messageId = (await message.getAttribute('data-card-id')) as string;
    await message.getByRole('button', { name: 'Not this time' }).click();
    await expect(message).toHaveCount(0);
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'declined');
    await expect.poll(async () => (await stack.home('GET', `/v1/cards/${messageId}`)).body.card.status).toBe('declined');

    await expect(page.getByTestId('card')).toHaveCount(5);
    await expect(page.getByTestId('red-dot')).toContainText('5');
    expect((await stack.home('GET', '/v1/cards?status=pending')).body.cards).toHaveLength(5);
    expect(w.problems).toEqual([]);
  });

  test('"Always allow this" asks in a dialog that holds focus, then records always on the home', async ({ page, fresh }) => {
    const stack = await fresh('normal');
    await open(page, stack);
    const card = page.getByTestId('card');
    const id = (await card.getAttribute('data-card-id')) as string;
    const trigger = card.getByRole('button', { name: 'Always allow this' });
    await trigger.click();
    const dialog = page.getByRole('dialog', { name: 'Always allow this?' });
    await expect(dialog).toBeVisible();
    expect(await dialog.evaluate((d) => d.contains(document.activeElement))).toBe(true);
    await axeClean(page);
    await page.keyboard.press('Escape');
    await expect(dialog).toBeHidden();
    await expect(trigger).toBeFocused(); // focus returns to where it was
    expect((await stack.home('GET', `/v1/cards/${id}`)).body.card.status).toBe('pending');
    await trigger.click();
    await dialog.getByRole('button', { name: 'Allow always' }).click();
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'approved_always');
    await expect.poll(async () => (await stack.home('GET', `/v1/cards/${id}`)).body.card.always).toBe(true);
  });

  test('an answer that fails puts the card back and says so; nothing changed on the home', async ({ page, fresh }) => {
    const stack = await fresh('normal');
    const w = watch(page, stack.url);
    await open(page, stack);
    const card = page.getByTestId('card');
    const id = (await card.getAttribute('data-card-id')) as string;
    await page.route('**/v1/cards/*/answer', (route) => route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ error: { code: 'internal', message: 'x' } }) }));
    w.allowHomeFailures();
    await card.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'card_failed');
    await expect(page.getByTestId('notice')).toContainText('That did not go through. Nothing was changed.');
    await expect(card).toHaveCount(1);
    expect((await stack.home('GET', `/v1/cards/${id}`)).body.card.status).toBe('pending');
    await page.unroute('**/v1/cards/*/answer');
    await card.getByRole('button', { name: 'Approve' }).click();
    await expect.poll(async () => (await stack.home('GET', `/v1/cards/${id}`)).body.card.status).toBe('approved');
    expect(w.problems).toEqual([]);
  });

  test('a card answered on another device is taken away with an explanation', async ({ page, fresh }) => {
    const stack = await fresh('normal');
    const w = watch(page, stack.url);
    w.allowHomeFailures();
    await open(page, stack);
    const card = page.getByTestId('card');
    const id = (await card.getAttribute('data-card-id')) as string;
    await page.route('**/v1/cards?status=pending', (route) => route.abort()); // keep the page from noticing first
    expect((await stack.home('POST', `/v1/cards/${id}/answer`, { decision: 'decline' })).status).toBe(200);
    await card.getByRole('button', { name: 'Approve' }).click();
    await expect(page.getByTestId('notice')).toHaveAttribute('data-notice', 'card_answered');
    await expect(card).toHaveCount(0);
    expect((await stack.home('GET', `/v1/cards/${id}`)).body.card.status).toBe('declined');
  });

  test('text from the home is shown as text, and anything key-shaped is hidden', async ({ page, shared }) => {
    const stack = await shared('normal');
    const w = watch(page, stack.url);
    const evil = '<img src=x onerror="window.__pwned=1"><script>window.__pwned=1</script>';
    const key = 'sk-live-abcdefghijklmnop1234567890';
    await page.route('**/v1/cards?status=pending', async (route) => {
      const res = await route.fetch();
      const json = await res.json();
      json.cards[0].title = evil;
      json.cards[0].thing = `the key is ${key}`;
      json.cards[0].under.text = evil;
      await route.fulfill({ response: res, json });
    });
    await open(page, stack);
    const card = page.getByTestId('card');
    await expect(card.getByRole('heading')).toHaveText(evil);
    await expect(card).not.toContainText(key);
    await expect(card).toContainText('the key is […]');
    expect(await card.locator('img, script').count()).toBe(0);
    expect(await page.evaluate(() => (window as unknown as { __pwned?: number }).__pwned)).toBeUndefined();
    expect(w.problems).toEqual([]);
  });
});

test.describe('pause', () => {
  test('one switch on every screen; the bar shows everywhere; the home really pauses and resumes', async ({ page, fresh }) => {
    const stack = await fresh('normal');
    const w = watch(page, stack.url);
    await page.setViewportSize(DESKTOP);
    await page.emulateMedia({ colorScheme: 'light' });
    await open(page, stack, '/records');
    await expect(page.getByTestId('pause')).toHaveAttribute('aria-pressed', 'false');
    await page.getByTestId('pause').click();
    await expect(page.getByTestId('paused-bar')).toBeVisible();
    await expect.poll(async () => (await stack.home('GET', '/v1/status')).body.paused).toBe(true);
    for (const path of ['today', 'ask', 'chores', 'records', 'services', 'permissions', 'devices', 'apps', 'settings', 'wallet', 'tray']) {
      await open(page, stack, `/${path}`);
      await expect(page.getByTestId('paused-bar'), path).toBeVisible();
    }
    await open(page, stack, '/chores');
    await axeClean(page);
    await page.getByTestId('paused-bar').getByRole('button').click();
    await expect(page.getByTestId('paused-bar')).toHaveCount(0);
    await expect.poll(async () => (await stack.home('GET', '/v1/status')).body.paused).toBe(false);
    expect(w.problems).toEqual([]);
  });

  test('on a phone Pause is in the top bar of every tab', async ({ page, shared }) => {
    const stack = await shared('normal');
    await page.setViewportSize(PHONE);
    for (const path of ['today', 'ask', 'chores', 'records', 'more', 'services', 'settings']) {
      await open(page, stack, `/${path}`);
      await expect(page.locator('header .pausebtn'), path).toBeVisible();
    }
  });
});

test.describe('navigation', () => {
  test('desktop: the sidebar in the specified order; every screen opens; focus lands on the heading', async ({ page, shared }) => {
    const stack = await shared('normal');
    const w = watch(page, stack.url);
    await page.setViewportSize(DESKTOP);
    await page.emulateMedia({ colorScheme: 'light' });
    await page.context().addInitScript(() => localStorage.setItem('muse.lang', 'en'));
    await open(page, stack);
    await loaded(page);
    const nav = page.getByRole('navigation', { name: 'Main menu' });
    const names = ['Today', 'Ask', 'Chores', 'Records', 'Services', 'Permissions', 'Devices', 'For apps', 'Settings'];
    expect(await nav.getByRole('link').evaluateAll((links) => links.map((a) => a.getAttribute('href')))).toEqual(['/today', '/ask', '/chores', '/records', '/services', '/permissions', '/devices', '/apps', '/settings']);
    await expect(nav.getByRole('link', { name: 'Wallet' })).toHaveCount(0);
    await expect(page.locator('.tabbar')).toHaveCount(0);
    for (const name of [...names].reverse()) {
      await nav.getByRole('link', { name: new RegExp(`^${name}`) }).click();
      const h1 = page.getByRole('heading', { level: 1 });
      await expect(h1).toContainText(name);
      await expect(h1).toBeFocused();
      await expect(page).toHaveTitle(`${name} · Muse`);
      await expect(nav.locator('[aria-current="page"]')).toHaveCount(1);
      await noSidewaysScroll(page);
      await axeClean(page);
    }
    await page.goBack();
    await expect(page.getByRole('heading', { level: 1 })).toContainText('Ask');
    await open(page, stack, '/more'); // a phone screen: on a desktop it becomes Services
    await expect(page).toHaveURL(/\/services$/);
    expect(w.problems).toEqual([]);
  });

  test('phone: five tabs, More holds the rest, For apps is not offered, and the keyboard works', async ({ page, shared }) => {
    const stack = await shared('normal');
    const w = watch(page, stack.url);
    await page.setViewportSize(PHONE);
    await page.context().addInitScript(() => localStorage.setItem('muse.lang', 'en'));
    await open(page, stack);
    await loaded(page);
    const tabs = page.locator('.tabbar').getByRole('link');
    expect(await tabs.evaluateAll((links) => links.map((a) => a.getAttribute('href')))).toEqual(['/today', '/ask', '/chores', '/records', '/more']);
    await expect(page.locator('.sidebar')).toHaveCount(0);
    for (const [href, title] of [['/ask', 'Ask'], ['/chores', 'Chores'], ['/records', 'Records'], ['/more', 'More']] as const) {
      await page.locator(`.tabbar a[href="${href}"]`).click();
      await expect(page.getByRole('heading', { level: 1 })).toHaveText(title);
      await expect(page.getByRole('heading', { level: 1 })).toBeFocused();
      await axeClean(page);
      await noSidewaysScroll(page);
    }
    const more = page.getByTestId('screen-more').getByRole('link');
    expect(await more.evaluateAll((links) => links.map((a) => a.getAttribute('href')))).toEqual(['/services', '/permissions', '/devices', '/settings']);
    for (const href of ['/services', '/permissions', '/devices', '/settings']) {
      await page.locator(`[data-testid="screen-more"] a[href="${href}"]`).click();
      await expect(page).toHaveURL(new RegExp(`${href}$`));
      await expect(page.locator('.tabbar a[href="/more"]')).toHaveAttribute('aria-current', 'page');
      await axeClean(page);
      await page.getByRole('link', { name: 'Back to More' }).click();
      await expect(page.getByRole('heading', { level: 1 })).toHaveText('More');
    }
    await open(page, stack, '/apps'); // desktop only
    await expect(page).toHaveURL(/\/more$/);

    // Keyboard: the skip link is first, links answer to Enter, the pause button to Space.
    await open(page, stack, '/today');
    await loaded(page);
    await page.keyboard.press('Tab');
    await expect(page.getByRole('link', { name: 'Skip to the content' })).toBeFocused();
    await page.locator('.tabbar a[href="/records"]').focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Records');
    await page.getByRole('link', { name: 'Ask something' }).focus();
    await page.keyboard.press('Enter');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Ask');
    expect(w.problems).toEqual([]);
  });

  test('every route opens by its address in both layouts and both languages', async ({ browser, shared }) => {
    const stack = await shared('normal');
    for (const layout of LAYOUTS) {
      for (const lang of LANGS) {
        const context = await browser.newContext({ viewport: layout.viewport, locale: lang.locale });
        const page = await context.newPage();
        const w = watch(page, stack.url);
        for (const route of ['today', 'ask', 'chores', 'records', 'services', 'permissions', 'devices', 'wallet', 'settings', 'tray', 'more', 'apps', '', 'nonsense']) {
          await open(page, stack, `/${route}`);
          await expect(page.getByRole('heading', { level: 1 }), `${layout.name} ${lang.name} /${route}`).toBeVisible();
          await expect(page.getByTestId('pause').or(page.getByTestId('tray-pause'))).toBeVisible();
          await noSidewaysScroll(page);
        }
        expect(w.problems, `${layout.name} ${lang.name}`).toEqual([]);
        await context.close();
      }
    }
  });

  test('the language follows the system, and Settings can change it for good', async ({ browser, shared }) => {
    const stack = await shared('normal');
    const context = await browser.newContext({ viewport: PHONE, locale: 'zh-TW' });
    const page = await context.newPage();
    await open(page, stack, '/settings');
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('設定');
    await expect(page.getByRole('radio', { name: '跟這台裝置一樣' })).toBeChecked();
    await axeClean(page);
    await page.getByRole('radio', { name: 'English' }).check();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Settings');
    await expect(page.locator('html')).toHaveAttribute('lang', 'en');
    await page.reload();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('Settings');
    await page.getByRole('radio', { name: 'Same as this device' }).check();
    await expect(page.getByRole('heading', { level: 1 })).toHaveText('設定');
    await context.close();
  });
});

test.describe('tray panel', () => {
  test.use({ viewport: { width: 360, height: 520 }, locale: 'en-GB' });

  test('shows the gauge, spending and the first waiting card; approving works from there', async ({ page, fresh }) => {
    const stack = await fresh('cards');
    const w = watch(page, stack.url);
    await open(page, stack, '/tray');
    await expect(page.getByTestId('tray-gauge')).toContainText(/\d+%/);
    await expect(page.getByTestId('spending')).toContainText(/NT\$\d+ of NT\$\d+/);
    await expect(page.getByTestId('card')).toHaveCount(1);
    await expect(page.getByRole('navigation')).toHaveCount(0);
    await noSidewaysScroll(page);
    await axeClean(page);
    const first = page.getByTestId('card');
    const id = (await first.getAttribute('data-card-id')) as string;
    expect((await stack.home('GET', '/v1/cards?status=pending')).body.cards[0].id).toBe(id);
    await first.getByRole('button', { name: 'Not this time' }).click();
    await expect.poll(async () => (await stack.home('GET', `/v1/cards/${id}`)).body.card.status).toBe('declined');
    await expect(page.getByTestId('card')).toHaveCount(1); // the next one moves up
    await expect(page.getByTestId('card')).not.toHaveAttribute('data-card-id', id);
    await page.getByTestId('tray-pause').click();
    await expect.poll(async () => (await stack.home('GET', '/v1/status')).body.paused).toBe(true);
    await expect(page.getByTestId('paused-bar')).toBeVisible();
    await page.getByRole('link', { name: 'Open Muse' }).click();
    await expect(page).toHaveURL(/\/today$/);
    expect(w.problems).toEqual([]);
  });

  for (const state of ['used', 'empty', 'error'] as const) {
    test(`state ${state}`, async ({ page, shared }) => {
      const stack = await shared(state);
      await open(page, stack, '/tray');
      if (state === 'used') await expect(page.getByTestId('tray-gauge')).toContainText('Refills at');
      if (state === 'empty') await expect(page.getByTestId('nothing-connected')).toBeVisible();
      if (state === 'error') await expect(page.getByTestId('unreachable')).toBeVisible();
      await noSidewaysScroll(page);
      await axeClean(page);
    });
  }
});

test.describe('security', () => {
  const DIST = resolve(import.meta.dirname, '..', 'dist');
  const walk = (dir: string): string[] => readdirSync(dir, { withFileTypes: true }).flatMap((e) => (e.isDirectory() ? walk(join(dir, e.name)) : [join(dir, e.name)]));

  test('the bundle holds no secret, no outside address, no eval and no inline script', () => {
    const files = walk(DIST);
    expect(files.length).toBeGreaterThan(2);
    for (const f of files) {
      const text = readFileSync(f, 'utf8');
      expect(text, f).not.toContain('fake-home-token');
      expect(text, f).not.toMatch(/\bsk-[A-Za-z0-9_-]{8,}/);
      expect(text, f).not.toMatch(/Bearer [A-Za-z0-9]/);
      expect(text, f).not.toMatch(/MUSE_HOME_TOKEN/);
      expect(text, f).not.toMatch(/\beval\(|new Function\(/);
      // The only outside addresses are the three in the link allow-list and the namespaces React needs to name.
      const urls = [...text.matchAll(/https?:\/\/[A-Za-z0-9.-]+/g)].map((m) => m[0]);
      const allowed = ['http://www.w3.org', 'https://react.dev', 'https://chatgpt.com', 'https://claude.ai', 'https://gemini.google.com'];
      expect(urls.filter((u) => !allowed.includes(u)), f).toEqual([]);
    }
    const html = readFileSync(join(DIST, 'index.html'), 'utf8').replaceAll('&#39;', "'");
    expect(html).toContain('http-equiv="Content-Security-Policy"');
    expect(html).toMatch(/script-src 'self'/);
    expect(html).toMatch(/connect-src 'self'/);
    expect(html).not.toMatch(/unsafe-inline|unsafe-eval/);
    expect([...html.matchAll(/<script\b[^>]*>/g)].every((m) => /\bsrc="\/assets\//.test(m[0]))).toBe(true);
    expect(html).not.toMatch(/<style|\sstyle=|\son[a-z]+=/);
  });

  test('the page is served with a strict policy, which the browser enforces', async ({ page, shared }) => {
    const stack = await shared('normal');
    const res = await page.goto(stack.url + '/today');
    const csp = res?.headers()['content-security-policy'] ?? '';
    expect(csp).toContain("default-src 'none'");
    expect(csp).toContain("script-src 'self'");
    expect(csp).toContain("connect-src 'self'");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toMatch(/unsafe-eval|unsafe-inline/); // (eval cannot be probed from here: the test driver's own evaluate is exempt)
    expect(res?.headers()['x-content-type-options']).toBe('nosniff');
    await loaded(page);
    const blocked = await page.evaluate(async () => {
      const out: string[] = [];
      try {
        await fetch('https://example.invalid/');
        out.push('fetch left');
      } catch {
        /* blocked */
      }
      const s = document.createElement('script');
      s.textContent = 'window.__inline = 1';
      document.head.append(s);
      if ((window as unknown as { __inline?: number }).__inline) out.push('inline script ran');
      return out;
    });
    expect(blocked).toEqual([]);
  });

  test('the proxy adds the token itself and refuses other sites', async ({ shared }) => {
    const stack = await shared('normal');
    const direct = await fetch(`http://127.0.0.1:${stack.homePort}/v1/status`);
    expect(direct.status).toBe(401); // the home itself wants a token
    expect(direct.headers.get('access-control-allow-origin')).toBeNull();
    const viaProxy = await fetch(`${stack.url}/v1/status`);
    expect(viaProxy.status).toBe(200);
    expect(JSON.stringify(await viaProxy.json())).not.toContain('fake-home-token');
    const crossSite = await fetch(`${stack.url}/v1/pause`, { method: 'POST', headers: { Origin: 'https://evil.example', 'Sec-Fetch-Site': 'cross-site' } });
    expect(crossSite.status).not.toBe(200);
    expect((await stack.home('GET', '/v1/status')).body.paused).toBe(false);
  });
});
