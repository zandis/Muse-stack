import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { App } from '../src/App';
import { Card, afterPayment } from '../src/components/Card';
import { Choices, firstOpenable } from '../src/components/Choices';
import { estimateDashes } from '../src/components/Gauge';
import { moreItems } from '../src/components/Shell';
import { HomeError, type HomeClient } from '../src/home/client';
import { CARD_KINDS, type Card as CardData, type Job, type Status } from '../src/home/types';
import { I18nProvider, translate } from '../src/i18n';
import { parseRoute } from '../src/router';
import { jobLines } from '../src/screens/Today';
import { APP_REPLIES, pretendAppClient } from './app-fixtures';
import { CARDS, NOW, job, provider, status } from './fixtures';

function setWidth(desktop: boolean) {
  window.matchMedia = ((query: string) => ({
    matches: desktop,
    media: query,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
  })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState(null, '', '/today');
  setWidth(false);
  // jsdom has no <dialog> methods
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute('open');
  };
});
afterEach(cleanup);

function renderCard(card: CardData, props: Partial<Parameters<typeof Card>[0]> = {}, lang: 'en' | 'zh-TW' = 'en') {
  const onAnswer = vi.fn();
  render(
    <I18nProvider initial={lang}>
      <Card card={card} now={NOW} onAnswer={onAnswer} {...props} />
    </I18nProvider>,
  );
  return onAnswer;
}

describe('Card', () => {
  it.each(CARD_KINDS)('%s: the shared shape, with cost always present', (kind) => {
    const card = CARDS[kind];
    const onAnswer = renderCard(card);
    const el = screen.getByRole('article', { name: card.title });
    expect(within(el).getAllByText(card.thing).length).toBeGreaterThan(0);
    expect(within(el).getByText('Under:')).toBeTruthy();
    expect(within(el).getByText('Cost:')).toBeTruthy();
    expect(within(el).getByTestId('card-expiry').textContent).toMatch(/^Expires in /);
    fireEvent.click(within(el).getByRole('button', { name: 'Approve' }));
    fireEvent.click(within(el).getByRole('button', { name: 'Not this time' }));
    expect(onAnswer.mock.calls).toEqual([
      ['approve', false],
      ['decline', false],
    ]);
  });

  it('save a file: says it costs nothing, shows the size, offers Always allow', () => {
    renderCard(CARDS.save_file);
    expect(screen.getByText('nothing')).toBeTruthy();
    expect(screen.getByText('18 kB')).toBeTruthy();
    expect(screen.getByText('Expires in 23 hours if not answered.')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'Always allow this' })).toBeTruthy();
  });

  it('message: the first lines with See all, and whether it is sent', () => {
    renderCard(CARDS.message);
    expect(screen.getByText('Hi Mei, Saturday works for us.')).toBeTruthy();
    expect(screen.queryByText('Shall we say 6 pm at your place?')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'See all' }));
    expect(screen.getByText('Shall we say 6 pm at your place?')).toBeTruthy();
    expect(screen.getByRole('button', { name: 'See less' }).getAttribute('aria-expanded')).toBe('true');
    expect(screen.getByText('It is sent once you approve.')).toBeTruthy();
    expect(screen.getByText('Mei')).toBeTruthy();
  });

  it('message draft: says it is not sent', () => {
    renderCard({ ...CARDS.message, extra: { preview: 'Dear Mr Lin', draft: true } } as CardData);
    expect(screen.getByText('Saved as a draft. It is not sent.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'See all' })).toBeNull();
  });

  it('spend: the amount in local currency, what is left of the limit, US dollars in details, five-minute expiry', () => {
    renderCard(CARDS.spend);
    expect(screen.getByText('NT$6')).toBeTruthy();
    expect(screen.getByText('After this, NT$18 of today’s limit is left.')).toBeTruthy();
    expect(screen.getByText('Expires in 4 minutes if not answered.')).toBeTruthy();
    expect(screen.queryByRole('button', { name: 'Always allow this' })).toBeNull();
    const details = screen.getByRole('button', { name: 'Details' });
    expect(details.getAttribute('aria-expanded')).toBe('false');
    fireEvent.click(details);
    expect(screen.getByText(/US\$0\.19/)).toBeTruthy();
  });

  it('spend over the limit: says by how much and cannot be approved', () => {
    renderCard({ ...CARDS.spend, extra: { payee: 'Deepgram', limit_left: { amount: '4.5', currency: 'TWD' } } } as CardData);
    expect(screen.getByText('This is NT$1.5 over today’s limit.')).toBeTruthy();
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true);
  });

  it('grant a permission: the sentence, the limits, the end date', () => {
    renderCard(CARDS.grant_permission);
    expect(screen.getByText('Every Monday at 08:00, read these three feeds.')).toBeTruthy();
    expect(screen.getByText('Reads 3 feeds you chose')).toBeTruthy();
    expect(screen.getByText(/until 13 Feb 2027/)).toBeTruthy();
  });

  it('grant a permission with no end date', () => {
    const c = CARDS.grant_permission as Extract<CardData, { kind: 'grant_permission' }>;
    renderCard({ ...c, extra: { ...c.extra, ends_at: null } });
    expect(screen.getByText('until you stop it')).toBeTruthy();
  });

  it('one-off credit: says it does not refill', () => {
    renderCard(CARDS.oneoff_credit);
    expect(screen.getByText('This credit does not refill.')).toBeTruthy();
    expect(screen.getByText('about 400 of 1,000 trial calls')).toBeTruthy();
  });

  it('send something private: the badge it needs and the services that qualify', () => {
    renderCard(CARDS.send_private, {}, 'zh-TW');
    expect(screen.getByText('私密')).toBeTruthy();
    expect(screen.getByText('Groq和AI on this computer')).toBeTruthy();
  });

  it('install a skill: what it asks for line by line, who wrote it, the fingerprint in details', () => {
    renderCard(CARDS.install_skill);
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual(['Read web pages you list', 'Save files to Documents/Muse']);
    expect(screen.getByText('Written by:')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Details' }));
    expect(screen.getByText(/9f2c4e1ab7d05c66/)).toBeTruthy();
  });

  it('shows text from the home as text, never as markup', () => {
    const evil = '<img src=x onerror="window.pwned=1"><script>window.pwned=1</script>';
    const { container } = render(
      <I18nProvider initial="en">
        <Card card={{ ...CARDS.save_file, title: evil, thing: evil, where: evil, under: { text: evil, mandate_id: null } }} now={NOW} onAnswer={() => undefined} />
      </I18nProvider>,
    );
    expect(container.querySelector('img, script')).toBeNull();
    expect(screen.getAllByText(evil).length).toBeGreaterThan(0);
  });

  it('hides anything key-shaped that arrives in a card', () => {
    const key = 'sk-live-abcdefghijklmnop1234567890';
    const { container } = render(
      <I18nProvider initial="en">
        <Card card={{ ...CARDS.message, thing: `use ${key}`, extra: { preview: `token ${key}`, to: key } } as CardData} now={NOW} onAnswer={() => undefined} />
      </I18nProvider>,
    );
    expect(container.textContent).not.toContain(key);
    expect(container.textContent).toContain('[…]');
  });

  it('while paused, cannot be approved but can be declined', () => {
    const onAnswer = renderCard(CARDS.save_file, { paused: true });
    expect((screen.getByRole('button', { name: 'Approve' }) as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByText('Paused. Resume to approve.')).toBeTruthy();
    fireEvent.click(screen.getByRole('button', { name: 'Not this time' }));
    expect(onAnswer).toHaveBeenCalledWith('decline', false);
  });

  it('Always allow asks once more in a dialog, then approves with always', () => {
    const onAnswer = renderCard(CARDS.save_file);
    fireEvent.click(screen.getByRole('button', { name: 'Always allow this' }));
    const dialog = screen.getByRole('dialog', { name: 'Always allow this?' });
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    expect(onAnswer).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Always allow this' }));
    fireEvent.click(within(dialog).getByRole('button', { name: 'Allow always' }));
    expect(onAnswer).toHaveBeenCalledWith('approve', true);
  });

  it('the compact card (tray) has no Always allow', () => {
    renderCard(CARDS.save_file, { compact: true });
    expect(screen.queryByRole('button', { name: 'Always allow this' })).toBeNull();
  });

  it('says when less than a minute is left', () => {
    renderCard({ ...CARDS.save_file, expires_at: new Date(NOW + 20_000).toISOString() });
    expect(screen.getByText('Expires in less than a minute.')).toBeTruthy();
  });

  it('works out what is left after a payment', () => {
    expect(afterPayment({ amount: '6', currency: 'TWD' }, { amount: '24', currency: 'TWD' })).toBe(18);
    expect(afterPayment({ amount: '0.1', currency: 'TWD' }, { amount: '0.3', currency: 'TWD' })).toBe(0.2);
    expect(afterPayment({ amount: '6', currency: 'USD' }, { amount: '24', currency: 'TWD' })).toBeNull();
  });
});

describe('Choices (placeholder fed by data)', () => {
  const subs = [{ id: 'x', name: 'Evil', web_url: 'https://evil.example/' }, { id: 'chatgpt', name: 'ChatGPT', web_url: 'https://chatgpt.com/' }];
  it('offers only the real choices, in order', () => {
    render(
      <I18nProvider initial="en">
        <Choices refillAt={Date.parse('2026-11-13T08:00:00Z')} subscriptions={subs} />
      </I18nProvider>,
    );
    const labels = within(screen.getByTestId('choices')).getAllByRole('button').map((b) => b.textContent);
    expect(labels).toHaveLength(2);
    expect(labels[0]).toMatch(/^Wait until \d\d:\d\d$/);
    expect(labels[1]).toBe('Open in my ChatGPT');
  });
  it('shows Spend only when given an estimate, and Wait alone when there is no subscription', () => {
    render(
      <I18nProvider initial="en">
        <Choices refillAt={null} subscriptions={[]} spendEstimate={{ amount: '1.3', currency: 'TWD' }} />
      </I18nProvider>,
    );
    expect(within(screen.getByTestId('choices')).getAllByRole('button').map((b) => b.textContent)).toEqual(['Wait for the refill', 'Spend about NT$1.3']);
  });
  it('opens the person’s own app only through the allow-list', async () => {
    const open = vi.fn(async () => true);
    render(
      <I18nProvider initial="en">
        <Choices refillAt={null} subscriptions={subs} open={open} />
      </I18nProvider>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Open in my ChatGPT' }));
    await waitFor(() => expect(screen.getByTestId('choice-result').textContent).toBe('Opened your ChatGPT. Nothing spent.'));
    expect(open).toHaveBeenCalledWith('https://chatgpt.com/');
    expect(firstOpenable([{ id: 'x', name: 'X', web_url: 'http://chatgpt.com/' }])).toBeNull();
  });
});

describe('small rules', () => {
  it('draws an estimate in dashes that stop at the value', () => {
    expect(estimateDashes(0)).toBe('0 100');
    const used = estimateDashes(50).split(' ').slice(0, -2).reduce((a, b) => a + Number(b), 0);
    expect(used).toBeGreaterThan(44);
    expect(used).toBeLessThanOrEqual(50);
  });
  it('hides Wallet until it exists and keeps For apps to the desktop', () => {
    expect(moreItems(false)).toEqual(['services', 'permissions', 'devices', 'settings']);
    expect(moreItems(true)).toEqual(['services', 'permissions', 'devices', 'apps', 'settings']);
    expect(moreItems(true, true)).toEqual(['services', 'permissions', 'devices', 'wallet', 'apps', 'settings']);
  });
  it('reads routes, sending anything unknown to Today', () => {
    expect(parseRoute('/')).toBe('today');
    expect(parseRoute('/records')).toBe('records');
    expect(parseRoute('/tray/')).toBe('tray');
    expect(parseRoute('/nope')).toBe('today');
    expect(parseRoute('/apps/extra')).toBe('apps');
  });
  it('lists running and done-today jobs in plain words', () => {
    const t = (k: Parameters<typeof translate>[1], ...a: (string | number)[]) => translate('en', k, ...a);
    const jobs: Job[] = [
      job({ id: 'a', status: 'queued' }),
      job({ id: 'b', status: 'running' }),
      job({ id: 'c', status: 'running', kind: 'batch', progress: 3 }),
      job({ id: 'd', status: 'deferred', next_attempt_at: '2026-11-13T06:20:00Z' }),
      job({ id: 'e', status: 'done', finished_at: '2026-11-13T02:15:00Z', kind: 'file-summary' }),
      job({ id: 'f', status: 'done', finished_at: '2026-11-12T10:00:00Z' }), // yesterday
      job({ id: 'g', status: 'failed', finished_at: '2026-11-13T03:00:00Z' }),
      job({ id: 'h', status: 'cancelled', finished_at: '2026-11-13T03:00:00Z' }),
    ];
    const { running, done } = jobLines(jobs, NOW, false, t, 'en', 'Asia/Taipei');
    expect(running.map((l) => l.right)).toEqual(['waiting its turn', 'working', '3 done', 'waits until 14:20']);
    expect(done.map((l) => [l.label, l.right])).toEqual([
      ['job_prompt', 'could not finish · 11:00'],
      ['job_file', '10:15'],
    ]);
    expect(jobLines(jobs, NOW, true, t, 'en', 'Asia/Taipei').running.every((l) => l.right === 'Paused')).toBe(true);
  });
});

// ---- the whole app against a pretend home

interface Pretend extends HomeClient {
  state: { status: Status; cards: CardData[]; jobs: Job[]; fail: HomeError | null; failAnswer: HomeError | null };
  calls: string[];
}

function pretendHome(over: Partial<Pretend['state']> = {}): Pretend {
  const state = { status: status(), cards: [CARDS.save_file], jobs: [job({ status: 'running' })], fail: null, failAnswer: null, ...over };
  const calls: string[] = [];
  const guard = () => {
    if (state.fail) throw state.fail;
  };
  return {
    state,
    calls,
    async status() {
      guard();
      return state.status;
    },
    async cards() {
      guard();
      return { cards: state.cards, counts: {} };
    },
    async jobs() {
      guard();
      return state.jobs;
    },
    async apps() {
      return [];
    },
    async answerCard(id, decision, always) {
      calls.push(`answer ${id} ${decision} ${String(Boolean(always))}`);
      if (state.failAnswer) throw state.failAnswer;
      state.cards = state.cards.filter((c) => c.id !== id);
      return { ...CARDS.save_file, status: 'approved' };
    },
    async pause() {
      calls.push('pause');
      guard();
      state.status = { ...state.status, paused: true };
    },
    async resume() {
      calls.push('resume');
      state.status = { ...state.status, paused: false };
    },
  };
}

async function renderApp(home: Pretend, lang: 'en' | 'zh-TW' = 'en', appClient = pretendAppClient()) {
  localStorage.setItem('muse.lang', lang);
  render(<App client={home} appClient={appClient} now={() => NOW} />);
  await act(async () => {
    await Promise.resolve();
  });
}

/** The pretend app client, with the home's first-run state set. */
function withFirstRun(firstRun: 'pending' | 'done' | 'skipped' | undefined, calls: string[] = []) {
  const settings = (APP_REPLIES['GET /v1/settings'] as { settings: Record<string, unknown> }).settings;
  return pretendAppClient(
    {
      ...APP_REPLIES,
      'GET /v1/settings': { settings: { ...settings, ...(firstRun ? { first_run: firstRun } : {}) } },
      'PATCH /v1/settings': (body: unknown) => ({ settings: { ...settings, ...(body as object) }, restart: { needed: false, proxy_running: false, command: 'muse restart' } }),
    },
    calls,
  );
}

describe('first run starts by itself', () => {
  const empty = () => pretendHome({ status: status({ providers: [provider({ configured: false })] }), cards: [], jobs: [] });

  it('opens /welcome on a fresh home with nothing connected, and "Skip for now" is written to the home', async () => {
    const calls: string[] = [];
    await renderApp(empty(), 'en', withFirstRun('pending', calls));
    expect(await screen.findByTestId('first-run')).toBeTruthy();
    expect(window.location.pathname).toBe('/welcome');
    fireEvent.click(screen.getByTestId('fr-skip'));
    await waitFor(() => expect(calls).toContain('PATCH /v1/settings {"first_run":"skipped"}'));
    expect(await screen.findByTestId('nothing-connected')).toBeTruthy(); // the empty state is still there after a skip
    expect(window.location.pathname).toBe('/today');
  });

  it('opening /welcome by hand writes nothing to the home until it is finished or skipped', async () => {
    const calls: string[] = [];
    window.history.replaceState(null, '', '/welcome');
    await renderApp(empty(), 'en', withFirstRun('pending', calls));
    await screen.findByTestId('first-run');
    expect(calls.filter((c) => c.startsWith('PATCH'))).toEqual([]);
  });

  it('does not open when a service is connected, when the home says it is over, or when the home does not say', async () => {
    for (const [connected, firstRun] of [
      [true, 'pending'],
      [false, 'done'],
      [false, 'skipped'],
      [false, undefined],
    ] as const) {
      const calls: string[] = [];
      window.history.replaceState(null, '', '/today');
      await renderApp(connected ? pretendHome() : empty(), 'en', withFirstRun(firstRun, calls));
      await screen.findByTestId(connected ? 'gauge-card' : 'nothing-connected');
      await act(async () => {
        await new Promise((r) => setTimeout(r, 20));
      });
      expect(screen.queryByTestId('first-run'), String(firstRun)).toBeNull();
      expect(window.location.pathname).toBe('/today');
      cleanup();
    }
  });

  it('does not open again on a device that already finished it, even before the home answers', async () => {
    localStorage.setItem('muse.firstRunDone', '1');
    const calls: string[] = [];
    await renderApp(empty(), 'en', withFirstRun('pending', calls));
    await screen.findByTestId('nothing-connected');
    expect(calls).not.toContain('GET /v1/settings');
    expect(window.location.pathname).toBe('/today');
  });
});

describe('Today', () => {
  it('shows the gauge, next refill, spending and that nothing was spent', async () => {
    await renderApp(pretendHome());
    await screen.findByTestId('gauge-percent');
    expect(screen.getByTestId('gauge-percent').textContent).toBe('80%');
    expect(screen.getByTestId('next-refill').textContent).toMatch(/^Next refill \d\d:\d\d \(Gemini\)$/);
    expect(screen.getByText('NT$0 of NT$32')).toBeTruthy();
    expect(screen.getByText('Nothing spent today.')).toBeTruthy();
    expect(screen.getByRole('link', { name: /^Free allowance today\s*80%\s*left.*Open Services$/ }).getAttribute('href')).toBe('/services');
    expect(screen.queryByText('about')).toBeNull();
  });

  it('marks an estimated gauge "about" and shows US dollars in details', async () => {
    const st = status({ paid: { cap_usd: 1, spent_usd: 0.03 }, providers: [provider({ limits: [{ window: 'day', metric: 'requests', limit: 100, remaining: 50, reset_at: null, source: 'estimate' }] })] });
    await renderApp(pretendHome({ status: st }));
    await screen.findByTestId('gauge-percent');
    expect(screen.getByText('about')).toBeTruthy();
    expect(screen.getByText('About NT$1 spent today.')).toBeTruthy();
    fireEvent.click(within(screen.getByTestId('spending')).getByRole('button', { name: 'Details' }));
    expect(screen.getByText(/US\$0\.03 of US\$1\.00/)).toBeTruthy();
  });

  it('nothing connected, first run skipped: the gauge is replaced by "Connect your first service", and there is no tip', async () => {
    const calls: string[] = [];
    await renderApp(pretendHome({ status: status({ providers: [provider({ configured: false })] }), cards: [], jobs: [] }), 'en', withFirstRun('skipped', calls));
    const box = await screen.findByTestId('nothing-connected');
    await waitFor(() => expect(calls).toContain('GET /v1/settings'));
    expect(window.location.pathname).toBe('/today'); // skipped once: Today's empty state stays
    expect(within(box).getByRole('heading', { name: 'Connect your first service' })).toBeTruthy();
    expect(within(box).getByRole('link', { name: 'Connect a service' }).getAttribute('href')).toBe('/welcome');
    expect(screen.queryByTestId('tip')).toBeNull();
    expect(screen.getByText('Nothing needs you right now.')).toBeTruthy();
    expect(screen.getByText('Nothing is running.')).toBeTruthy();
    expect(screen.getByText(/Nothing yet today/)).toBeTruthy();
  });

  it('all used up: gauge at zero, "Refills at", and the choices', async () => {
    const st = status({ providers: [provider({ state: 'exhausted', limits: [{ window: 'day', metric: 'requests', limit: 100, remaining: 0, reset_at: '2026-11-13T08:00:00Z', source: 'header' }] })] });
    await renderApp(pretendHome({ status: st }));
    await screen.findByTestId('choices');
    expect(screen.getByTestId('gauge-percent').textContent).toBe('0%');
    expect(screen.getByTestId('next-refill').textContent).toMatch(/^Refills at \d\d:\d\d$/);
  });

  it('paused: the bar with Resume, and resuming calls the home', async () => {
    const home = pretendHome({ status: status({ paused: true }) });
    await renderApp(home);
    const bar = await screen.findByTestId('paused-bar');
    expect(within(bar).getByText('Everything is paused')).toBeTruthy();
    expect(screen.getByTestId('pause').getAttribute('aria-pressed')).toBe('true');
    fireEvent.click(within(bar).getByRole('button', { name: 'Resume' }));
    await waitFor(() => expect(screen.queryByTestId('paused-bar')).toBeNull());
    expect(home.calls).toContain('resume');
  });

  it('pause shows at once and is put back if the home refuses', async () => {
    const home = pretendHome();
    await renderApp(home);
    await screen.findByTestId('gauge-percent');
    home.state.fail = new HomeError('api', { code: 'internal', status: 500 });
    fireEvent.click(screen.getByTestId('pause'));
    expect(screen.getByTestId('paused-bar')).toBeTruthy(); // optimistic
    await waitFor(() => expect(screen.queryByTestId('paused-bar')).toBeNull());
    expect(screen.getByTestId('notice').getAttribute('data-notice')).toBe('pause_failed');
  });

  it('home unreachable before anything is known: the error pattern with Retry and "Use this device for now"', async () => {
    const home = pretendHome({ fail: new HomeError('unreachable') });
    await renderApp(home);
    const box = await screen.findByTestId('unreachable');
    expect(within(box).getByText('Can’t reach your always-on helper.')).toBeTruthy();
    expect(within(box).getByText('Wardmuse tries again by itself.')).toBeTruthy();
    fireEvent.click(within(box).getByRole('button', { name: 'Use this device for now' }));
    expect(screen.getByTestId('using-this-device')).toBeTruthy();
    home.state.fail = null;
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await screen.findByTestId('gauge-percent');
    expect(screen.queryByTestId('using-this-device')).toBeNull();
  });

  it('home unreachable later: keeps the last known content and says when it was last seen', async () => {
    const home = pretendHome();
    await renderApp(home);
    await screen.findByTestId('gauge-percent');
    home.state.fail = new HomeError('unreachable');
    fireEvent.click(screen.getByTestId('pause')); // any action makes the app ask again
    const box = await screen.findByTestId('unreachable');
    expect(box.textContent).toMatch(/Last seen \d\d:\d\d\./);
    expect(screen.getByTestId('gauge-percent')).toBeTruthy();
    expect(localStorage.getItem('muse.lastSeen')).toMatch(/^\d+$/);
  });

  it('a refused device and any other error follow the same pattern', async () => {
    await renderApp(pretendHome({ fail: new HomeError('api', { code: 'unauthorized', status: 401 }) }));
    expect((await screen.findByTestId('home-error')).textContent).toContain('This device is not allowed to talk to your helper.');
    cleanup();
    await renderApp(pretendHome({ fail: new HomeError('protocol') }));
    const box = await screen.findByTestId('home-error');
    expect(box.textContent).toContain('Something went wrong while talking to your helper.');
    expect(within(box).getByRole('button', { name: 'Retry' })).toBeTruthy();
  });

  it('loading: says "Updating…" and never a blank screen', async () => {
    const home = pretendHome();
    let release: () => void = () => undefined;
    const gate = new Promise<void>((r) => (release = r));
    const real = home.status.bind(home);
    home.status = async () => {
      await gate;
      return real();
    };
    await renderApp(home);
    expect(screen.getByTestId('updating').textContent).toBe('Updating…');
    expect(screen.getByRole('heading', { level: 1, name: /Today/ })).toBeTruthy();
    await act(async () => {
      release();
      await gate;
    });
    await screen.findByTestId('gauge-percent');
    expect(screen.queryByTestId('updating')).toBeNull();
  });

  it('approving removes the card at once and tells the home', async () => {
    const home = pretendHome();
    await renderApp(home);
    const card = await screen.findByTestId('card');
    expect(screen.getByTestId('red-dot').textContent).toContain('1 waiting for your decision');
    fireEvent.click(within(card).getByRole('button', { name: 'Approve' }));
    expect(screen.queryByTestId('card')).toBeNull(); // before the home has answered
    await waitFor(() => expect(screen.getByTestId('notice').getAttribute('data-notice')).toBe('approved'));
    expect(home.calls).toEqual([`answer ${CARDS.save_file.id} approve false`]);
    expect(screen.queryByTestId('red-dot')).toBeNull();
  });

  it('puts the card back when the answer fails', async () => {
    const home = pretendHome({ failAnswer: new HomeError('unreachable') });
    await renderApp(home);
    fireEvent.click(within(await screen.findByTestId('card')).getByRole('button', { name: 'Not this time' }));
    await waitFor(() => expect(screen.getByTestId('notice').getAttribute('data-notice')).toBe('card_failed'));
    expect(screen.getByTestId('card')).toBeTruthy();
  });

  it('says so when the card was already answered elsewhere, or ran out of time', async () => {
    const home = pretendHome({ failAnswer: new HomeError('api', { code: 'card_already_answered', status: 409 }) });
    await renderApp(home);
    fireEvent.click(within(await screen.findByTestId('card')).getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(screen.getByTestId('notice').textContent).toContain('already answered on another device'));
    cleanup();
    const home2 = pretendHome({ failAnswer: new HomeError('api', { code: 'card_expired', status: 410 }) });
    await renderApp(home2);
    fireEvent.click(within(await screen.findByTestId('card')).getByRole('button', { name: 'Approve' }));
    await waitFor(() => expect(screen.getByTestId('notice').getAttribute('data-notice')).toBe('card_expired'));
  });

  it('shows one tip, and dismissing it lasts the day', async () => {
    await renderApp(pretendHome());
    const tip = await screen.findByTestId('tip');
    expect(tip.textContent).toContain('Tip: connect Cerebras (+4 min)');
    fireEvent.click(within(tip).getByRole('button', { name: 'Dismiss tip' }));
    expect(screen.queryByTestId('tip')).toBeNull();
    cleanup();
    await renderApp(pretendHome());
    await screen.findByTestId('gauge-percent');
    expect(screen.queryByTestId('tip')).toBeNull();
  });

  it('is in Traditional Chinese when that is the language', async () => {
    await renderApp(pretendHome(), 'zh-TW');
    await screen.findByTestId('gauge-percent');
    expect(screen.getByRole('heading', { level: 1 }).textContent).toContain('今天');
    expect(screen.getByText('今天的免費額度')).toBeTruthy();
    expect(screen.getByText('等你決定（1）')).toBeTruthy();
    expect(document.documentElement.lang).toBe('zh-Hant-TW');
  });
});

describe('navigation and the other screens', () => {
  it('phone: five tabs in order, with More holding the rest', async () => {
    await renderApp(pretendHome());
    const nav = screen.getByRole('navigation', { name: 'Main menu' });
    expect(within(nav).getAllByRole("link").map((a) => a.lastChild?.textContent)).toEqual(["Today", "Ask", "Chores", "Records", "More"]);
    fireEvent.click(within(nav).getByRole('link', { name: 'More' }));
    expect(screen.getByRole('heading', { level: 1, name: 'More' })).toBe(document.activeElement);
    const list = screen.getByTestId('screen-more');
    expect(within(list).getAllByRole('link').map((a) => a.querySelector('b')?.textContent)).toEqual(['Services', 'Permissions', 'Devices', 'Settings']);
    expect(screen.getByText(/Wallet appears here/)).toBeTruthy();
    fireEvent.click(within(list).getByRole('link', { name: /Settings/ }));
    expect(window.location.pathname).toBe('/settings');
    expect(within(nav).getByRole('link', { name: 'More' }).getAttribute('aria-current')).toBe('page');
    expect(screen.getByRole('link', { name: 'Back to More' })).toBeTruthy();
  });

  it('desktop: the sidebar in the specified order, without Wallet', async () => {
    setWidth(true);
    await renderApp(pretendHome());
    const nav = screen.getByRole('navigation', { name: 'Main menu' });
    expect(within(nav).getAllByRole('link').map((a) => a.textContent?.replace(/\d.*$/, ''))).toEqual(['Today', 'Ask', 'Chores', 'Records', 'Services', 'Permissions', 'Devices', 'For apps', 'Settings']);
  });

  it('each placeholder says what will appear there and that it comes later; Pause stays reachable', async () => {
    for (const [path, title] of [
      ['/devices', 'Only this device so far'],
      ['/wallet', 'No payments yet'],
    ] as const) {
      window.history.replaceState(null, '', path);
      await renderApp(pretendHome());
      expect(screen.getByRole('heading', { level: 2, name: title })).toBeTruthy();
      expect(screen.getByText('This screen comes in a later build.')).toBeTruthy();
      expect(screen.getByTestId('pause')).toBeTruthy();
      cleanup();
    }
    window.history.replaceState(null, '', '/wallet');
    await renderApp(pretendHome());
    expect(screen.getByTestId('testnet-header').textContent).toContain('Nothing here is real money.');
  });

  it('the language switch in Settings changes the language and remembers it', async () => {
    window.history.replaceState(null, '', '/settings');
    await renderApp(pretendHome());
    fireEvent.click(screen.getByRole('radio', { name: '繁體中文' }));
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe('設定');
    expect(localStorage.getItem('muse.lang')).toBe('zh-TW');
    fireEvent.click(screen.getByRole('radio', { name: '跟這台裝置一樣' }));
    expect(localStorage.getItem('muse.lang')).toBeNull();
  });

  it('the tray panel shows the gauge, spending and the first waiting card only', async () => {
    window.history.replaceState(null, '', '/tray');
    await renderApp(pretendHome({ cards: [CARDS.spend, CARDS.save_file] }));
    await screen.findByTestId('tray-gauge');
    expect(screen.getByTestId('gauge-percent').textContent).toBe('80%');
    expect(screen.getByTestId('spending').textContent).toContain('NT$0 of NT$32');
    expect(screen.getAllByTestId('card')).toHaveLength(1);
    expect(screen.queryByRole('navigation')).toBeNull();
    expect(screen.getByRole('link', { name: 'Open Wardmuse' }).getAttribute('href')).toBe('/today');
  });
});
