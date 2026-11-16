// Records and Permissions (docs/spec/02-ux.md sections 4.4 and 4.6), against a pretend
// home, with the fixture's fixed clock: nothing here depends on today's date.
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { App } from '../src/App';
import type { HomeClient } from '../src/home/client';
import type { Card, HomeRecord, Permission, RecordsPage } from '../src/home/types';
import { translate, type Key } from '../src/i18n';
import type { Lang } from '../src/i18n/format';
import { proofOf } from '../src/screens/Records';
import { costWords, permissionLimits, permissionSentence, recordSentence, type WordsContext } from '../src/screens/trust-words';
import { APP_REPLIES, pretendAppClient } from './app-fixtures';
import { CARDS, NOW, status } from './fixtures';

const T = (lang: Lang) => (key: Key, ...args: (string | number)[]) => translate(lang, key, ...args);
const ID = (c: string) => `m1_${c.repeat(64)}`;
const P_SPEND = ID('a');
const P_PROG = ID('b');
const P_ENDED = ID('c');
const P_FOLDER = ID('d');
const at = (iso: string) => Date.parse(iso);

function must<V>(v: V | null | undefined): V {
  if (v === null || v === undefined) throw new Error('missing');
  return v;
}

function byPerm(cards: HTMLElement[], name: string): HTMLElement {
  return must(cards.find((c) => c.getAttribute('data-perm') === name));
}

function rec(seq: number, over: Partial<HomeRecord>): HomeRecord {
  return {
    v: 1,
    device: 'f'.repeat(64),
    seq,
    time: at('2026-11-13T03:00:00Z'),
    mandate: null,
    actor: 'person',
    action: 'home.pause',
    resource: null,
    summary_hash: '0'.repeat(64),
    outcome: 'ok',
    cost: null,
    prev: '1'.repeat(64),
    sig: '2'.repeat(128),
    summary: 'Something the home wrote',
    summary_salt: '3'.repeat(32),
    ...over,
  };
}

const RECORDS: HomeRecord[] = [
  rec(5, { time: at('2026-11-13T04:00:00Z'), action: 'card.answer', outcome: 'declined', egress: 'none', summary: 'Declined: spend money' }),
  rec(4, { time: at('2026-11-13T03:00:00Z'), action: 'home.pause', egress: 'none', summary: 'Paused the queue' }),
  (() => {
    const r = rec(3, { time: at('2026-11-13T02:00:00Z'), action: 'files.write', actor: 'helper', mandate: P_FOLDER, egress: 'none' });
    delete r.summary;
    delete r.summary_salt;
    return r;
  })(),
  rec(2, { time: at('2026-11-12T04:00:00Z'), action: 'llm.call', egress: 'service:deepseek', cost: { currency: 'USD', minor: 4 }, summary: 'Answered with a paid service' }),
  rec(1, { time: at('2026-11-12T03:00:00Z'), action: 'llm.call', actor: 'helper', mandate: P_PROG, egress: 'service:groq', summary: 'The program used Groq' }),
  rec(0, { time: at('2026-11-09T03:00:00Z'), action: 'mandate.issue', mandate: P_SPEND, egress: 'none', summary: 'Gave a permission' }),
];

function page(over: Partial<RecordsPage> = {}): RecordsPage {
  return { records: RECORDS, order: 'newest_first', next_before: null, verify: { ok: true, size: 6 }, sealed: { size: 5, root: 'ab'.repeat(32), time: '2026-11-13T04:30:00Z' }, device: 'f'.repeat(64), ...over };
}

function perm(id: string, over: Partial<Permission>): Permission {
  return {
    id,
    name: id.slice(3, 15),
    holder: 'helper:home',
    state: 'active',
    sentence: 'The home’s own sentence.',
    lines: ['A line the home wrote'],
    actions: [],
    resources: [],
    limits: [],
    privacy: 'private',
    issued_at: '2026-11-09T03:00:00Z',
    not_before: '2026-11-09T03:00:00Z',
    not_after: '2026-11-30T03:00:00Z',
    targets: { records: { state: 'active', reason: '' } },
    ...over,
  };
}

const SPEND = perm(P_SPEND, {
  actions: ['pay.spend'],
  resources: ['search.example.com', 'maps.example.com', 'translate.example.com'].map((value) => ({ kind: 'host', value })),
  limits: [{ kind: 'spend', unit: 'TWD', decimals: 2, period: 'day', amount: 3000 }],
  targets: { records: { state: 'active', reason: '' }, payments: { state: 'not_built', reason: 'Paying is not built yet.' } },
});
const PROG = perm(P_PROG, {
  holder: 'app:coding-tool',
  actions: ['llm.call'],
  resources: [{ kind: 'provider', value: 'router' }],
  limits: [{ kind: 'requests', unit: '', decimals: 0, period: 'day', amount: 200 }],
  not_after: '2027-01-10T03:00:00Z',
  targets: { router: { state: 'active', reason: '' }, records: { state: 'active', reason: '' } },
});
const ENDED = perm(P_ENDED, {
  state: 'expired',
  actions: ['calendar.read'],
  resources: [{ kind: 'calendar', value: 'Family' }],
  not_after: '2026-11-05T03:00:00Z',
  targets: { records: { state: 'revoked', reason: '' }, connectors: { state: 'revoked', reason: '' } },
});
const FOLDER = perm(P_FOLDER, {
  actions: ['files.read', 'llm.call'],
  resources: [
    { kind: 'folder', value: '/home/mei/Documents' },
    { kind: 'provider', value: 'router' },
  ],
  targets: { skills: { state: 'active', reason: '' }, records: { state: 'active', reason: '' } },
});

function setWidth(desktop: boolean) {
  window.matchMedia = ((query: string) => ({ matches: desktop, media: query, addEventListener: () => undefined, removeEventListener: () => undefined })) as unknown as typeof window.matchMedia;
}

beforeEach(() => {
  localStorage.clear();
  setWidth(false);
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute('open', '');
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute('open');
    this.dispatchEvent(new Event('close'));
  };
});
afterEach(cleanup);

function home(cards: Card[] = []): HomeClient {
  return {
    status: async () => status(),
    cards: async () => ({ cards, counts: {} }),
    answerCard: async (id) => ({ ...CARDS.grant_permission, id, status: 'approved' }),
    jobs: async () => [],
    apps: async () => [],
    pause: async () => undefined,
    resume: async () => undefined,
  };
}

interface Setup {
  records?: RecordsPage;
  calls?: string[];
  stop?: (n: number) => unknown;
  perms?: Permission[];
  cards?: Card[];
  lang?: Lang;
  path?: string;
  /** The home answers these calls with an error. */
  broken?: ('records' | 'permissions')[];
}

async function show({ records = page(), calls = [], stop, perms = [SPEND, PROG, ENDED, FOLDER], cards = [], lang = 'en', path = '/records', broken = [] }: Setup = {}) {
  let stops = 0;
  const list = { mandates: perms, enforcement: { skills: 'advise', skills_not_shipped_with_muse: 'enforce', router: 'enforce', not_built: ['payments'] } };
  const replies: Record<string, unknown> = {
    ...APP_REPLIES,
    'GET /v1/receipts?limit=100': records,
    'GET /v1/receipts?limit=1': records,
    'GET /v1/receipts?limit=200': records,
    [`GET /v1/receipts?limit=100&mandate=${P_PROG}`]: { ...records, records: records.records.filter((r) => r.mandate === P_PROG) },
    [`GET /v1/receipts?limit=20&mandate=${P_SPEND}`]: { ...records, records: records.records.filter((r) => r.mandate === P_SPEND) },
    [`GET /v1/mandates?lang=${lang}`]: list,
    [`GET /v1/mandates/declined?lang=${lang}`]: {
      declined: [{ card_id: '9'.repeat(16), device: 'helper-bot', holder: 'device:helper-bot', actions: ['llm.paid'], resources: [], sentence: 'Your device “helper-bot” may use paid AI services.', declined_at: '2026-11-12T03:00:00Z', ask_again_after: '2026-11-19T03:00:00Z' }],
    },
    'POST /v1/receipts/export': (body: unknown) => ({ export: { type: 'muse.records.export', body }, file_name: 'muse-records-2026-11-13.json', count: 6, size: 6, redacted: 6, device: 'f'.repeat(64), recorded: true }),
    [`POST /v1/mandates/${PROG.name}/stop`]: () => (stop ? stop(++stops) : { stopped: true, was: 'active', id: P_PROG, name: PROG.name, targets: { router: { state: 'revoked', reason: '' }, records: { state: 'revoked', reason: '' } }, complete: true, recorded: true }),
    [`POST /v1/mandates/${SPEND.name}/stop`]: () => ({ stopped: true, was: 'active', id: P_SPEND, name: SPEND.name, targets: { records: { state: 'revoked', reason: '' }, payments: { state: 'not_built', reason: 'x' } }, complete: true, recorded: true }),
  };
  const answered = Object.fromEntries(
    Object.entries(replies).filter(([k]) => !((broken.includes('records') && k.startsWith('GET /v1/receipts')) || (broken.includes('permissions') && k.startsWith('GET /v1/mandates')))),
  );
  localStorage.setItem('muse.lang', lang);
  window.history.replaceState(null, '', path);
  render(<App client={home(cards)} appClient={pretendAppClient(answered, calls)} now={() => NOW} />);
  await act(async () => {
    await Promise.resolve();
  });
}

const words = (lang: Lang, perms: Permission[] = [SPEND, PROG, FOLDER]): WordsContext => ({
  t: T(lang),
  lang,
  permissions: new Map(perms.map((p) => [p.id, p])),
  services: new Map([['groq', { name: 'Groq', badge: 'standard' as const }]]),
  money: undefined,
});

describe('the words', () => {
  it('a permission is one sentence, with its money in the person’s currency and the US-dollar figure in its limits', () => {
    expect(permissionSentence(SPEND, words('en'), true, NOW)).toBe('Your helper may spend up to NT$30 a day on 3 services, until 30 Nov.');
    expect(permissionSentence(SPEND, words('zh-TW'), true, NOW)).toBe('你的小幫手可以每天最多在 3 個服務上花 NT$30，到 11月30日 為止。');
    expect(permissionLimits(SPEND, words('en'))).toEqual(['Up to NT$30 a day (about US$0.94)', 'Content goes only to Private services']);
    expect(permissionSentence(PROG, words('en'), true, NOW)).toBe('The program “coding-tool” may use your allowances, up to 200 requests a day, until 10 Jan 2027.');
    expect(permissionSentence(FOLDER, words('en'), false, NOW)).toBe('Your helper may read the folder Documents and use your allowances.');
    expect(permissionSentence(perm(ID('e'), { actions: ['health.read'], sentence: 'Home words.' }), words('en'), true, NOW)).toBe('Home words.');
  });

  it('a record is one plain sentence: who, what; no action names, ids or hashes', () => {
    const w = words('en');
    expect(RECORDS.map((r) => recordSentence(r, w))).toEqual([
      'You declined a card.',
      'You paused everything.',
      'Your helper saved a file.',
      'You asked Deepseek.',
      'The program “coding-tool” asked Groq.',
      'You gave a permission.',
    ]);
    expect(recordSentence(must(RECORDS[4]), words('zh-TW'))).toBe('程式「coding-tool」問了 Groq。');
    expect(costWords(must(RECORDS[3]), w)).toEqual({ main: 'about NT$1.3', usd: 'US$0.04', usdPlain: 'US$0.04' });
    expect(costWords(must(RECORDS[0]), w).main).toBe('Nothing spent');
  });

  it('each record’s proof follows from the home’s check and its last seal', () => {
    expect(proofOf(must(RECORDS[5]), { ok: true, size: 6 }, 5)).toBe('sealed');
    expect(proofOf(must(RECORDS[0]), { ok: true, size: 6 }, 5)).toBe('unsealed');
    const bad = { ok: false, size: 6, kind: 'summary_mismatch', index: 4 };
    expect([must(RECORDS[0]), must(RECORDS[1]), must(RECORDS[2])].map((r) => proofOf(r, bad, 5))).toEqual(['after', 'bad', 'sealed']);
    expect(proofOf(must(RECORDS[2]), { ok: false, size: 6, kind: 'root_mismatch', index: null }, 5)).toBe('unknown');
  });
});

describe('Records', () => {
  it('lists every record newest first, grouped by day, each as one sentence with permission, cost and where it went', async () => {
    await show();
    await screen.findByTestId('records-ok');
    expect(screen.getByTestId('records-ok').textContent).toBe('All 6 records check out');
    const days = screen.getAllByRole('heading', { level: 3 }).map((h) => h.textContent);
    expect(days[0]).toMatch(/^Today · 13 Nov$/);
    expect(days[1]).toMatch(/^Yesterday · 12 Nov$/);
    expect(days).toHaveLength(3);
    const rows = screen.getAllByTestId('record-row');
    expect(rows.map((r) => r.getAttribute('data-seq'))).toEqual(['5', '4', '3', '2', '1', '0']);
    expect(must(rows[3]).textContent).toContain('You asked Deepseek.');
    expect(must(rows[3]).textContent).toContain('about NT$1.3');
    expect(must(rows[3]).textContent).toContain('Sent to Deepseek');
    expect(must(rows[4]).textContent).toContain('Under: The program “coding-tool” may use your allowances, up to 200 requests a day.');
    expect(must(rows[2]).textContent).toContain('What it said was removed');
    expect(must(rows[5]).textContent).toContain('Under: Your helper may spend up to NT$30 a day on 3 services.');
    expect(document.body.textContent).not.toMatch(/llm\.call|mandate|m1_|summary/);
  });

  it('a redacted record says so honestly in the record itself', async () => {
    await show();
    await screen.findByTestId('records-ok');
    fireEvent.click(must(screen.getAllByTestId('record-row')[2]));
    const sheet = await screen.findByTestId('record-detail');
    expect(within(sheet).getByTestId('record-said').textContent).toMatch(/^Removed on purpose\./);
    expect(within(sheet).getByTestId('record-proof').textContent).toBe('Checks out, and is sealed (13 Nov)');
    fireEvent.click(within(sheet).getByRole('button', { name: 'Details' }));
    expect(within(sheet).getByTestId('record-more').textContent).toContain('files.write'); // action names only behind Details
    fireEvent.click(within(sheet).getByTestId('record-close'));
    await waitFor(() => expect(document.activeElement).toBe(screen.getAllByTestId('record-row')[2]));
  });

  it('filters by kind, by who, and by date', async () => {
    await show();
    await screen.findByTestId('records-ok');
    fireEvent.click(screen.getByTestId('chip-cost'));
    expect(screen.getAllByTestId('record-row').map((r) => r.getAttribute('data-seq'))).toEqual(['2']);
    fireEvent.click(screen.getByTestId('chip-cost'));
    fireEvent.click(screen.getByTestId('chip-left'));
    expect(screen.getAllByTestId('record-row').map((r) => r.getAttribute('data-seq'))).toEqual(['2', '1']);
    fireEvent.click(screen.getByTestId('chip-left'));
    fireEvent.change(screen.getByTestId('filter-who'), { target: { value: 'p:The program “coding-tool”' } });
    expect(screen.getAllByTestId('record-row').map((r) => r.getAttribute('data-seq'))).toEqual(['1']);
    fireEvent.change(screen.getByTestId('filter-who'), { target: { value: 'all' } });
    fireEvent.change(screen.getByTestId('filter-when'), { target: { value: 'today' } });
    expect(screen.getAllByTestId('record-row').map((r) => r.getAttribute('data-seq'))).toEqual(['5', '4', '3']);
    fireEvent.change(screen.getByTestId('filter-when'), { target: { value: '7' } });
    expect(screen.getAllByTestId('record-row')).toHaveLength(6);
    fireEvent.click(screen.getByTestId('chip-changed'));
    fireEvent.click(screen.getByTestId('chip-cost'));
    expect(screen.getByTestId('records-none').textContent).toBe('No records match these filters.');
  });

  it('a chain that does not check out names the record, and Check my records asks the home again', async () => {
    const calls: string[] = [];
    await show({ calls, records: page({ verify: { ok: false, size: 6, kind: 'summary_mismatch', index: 4 } }) });
    const bad = await screen.findByTestId('records-bad');
    expect(bad.textContent).toContain('Record 5 does not match the ones around it.');
    expect(bad.textContent).toContain('What it says was changed after it was written.');
    const rows = screen.getAllByTestId('record-row');
    expect(must(rows[1]).textContent).toContain('Does not match');
    expect(must(rows[0]).textContent).toContain('Can’t be confirmed');
    fireEvent.click(screen.getByTestId('records-check'));
    await waitFor(() => expect(screen.getByTestId('records-check-note').textContent).toBe('Checked just now: record 5 does not match.'));
    expect(calls).toContain('GET /v1/receipts?limit=1');
  });

  it('Check my records says how many check out', async () => {
    await show();
    await screen.findByTestId('records-ok');
    fireEvent.click(screen.getByTestId('records-check'));
    await waitFor(() => expect(screen.getByTestId('records-check-note').textContent).toBe('Checked just now: all 6 records check out.'));
  });

  it('export asks whether to leave out what records say, then saves the home’s file', async () => {
    const calls: string[] = [];
    await show({ calls });
    await screen.findByTestId('records-ok');
    fireEvent.click(screen.getByTestId('records-export'));
    const dialog = screen.getByTestId('export-dialog');
    expect(dialog.hasAttribute('open')).toBe(true);
    fireEvent.click(within(dialog).getByTestId('export-redact'));
    fireEvent.click(within(dialog).getByTestId('export-go'));
    await waitFor(() => expect(within(dialog).getByTestId('export-note').textContent).toBe('Saved muse-records-2026-11-13.json: 6 records, without what they say.'));
    expect(calls).toContain('POST /v1/receipts/export {"redact_summaries":true}');
  });

  it('empty, offline and paused states', async () => {
    await show({ records: page({ records: [], verify: { ok: true, size: 0 }, sealed: null }) });
    expect((await screen.findByTestId('records-empty')).textContent).toContain('No records yet');
    cleanup();
    await show({ broken: ['records'] });
    await waitFor(() => expect(screen.getByTestId('records-problem').textContent).toContain('Can’t reach your home'));
  });

  it('desktop: a table with the same columns, and the selected record in a pane', async () => {
    setWidth(true);
    await show();
    const table = await screen.findByTestId('records-table');
    expect(within(must(table.querySelector('thead'))).getAllByRole('columnheader').map((h) => h.textContent)).toEqual(['Time', 'What', 'Permission', 'Cost', 'Where it went']);
    fireEvent.click(within(table).getByRole('button', { name: 'You asked Deepseek.' }));
    const pane = screen.getByTestId('record-pane');
    expect(within(pane).getByRole('heading', { level: 2 }).textContent).toBe('You asked Deepseek.');
    expect(within(pane).getByText('about NT$1.3')).toBeTruthy();
  });

  it('opened from a permission: only its records, said plainly, with a way back to all', async () => {
    const calls: string[] = [];
    await show({ calls, path: `/records?permission=${P_PROG}` });
    await waitFor(() => expect(screen.getAllByTestId('record-row').map((r) => r.getAttribute('data-seq'))).toEqual(['1']));
    expect(screen.getByTestId('records-under').textContent).toContain('Only records under: The program “coding-tool” may use your allowances');
    expect(calls).toContain(`GET /v1/receipts?limit=100&mandate=${P_PROG}`);
    fireEvent.click(within(screen.getByTestId('records-under')).getByRole('button', { name: 'Show all records' }));
    await waitFor(() => expect(screen.getAllByTestId('record-row')).toHaveLength(6));
  });

  it('zh-TW', async () => {
    await show({ lang: 'zh-TW' });
    expect((await screen.findByTestId('records-ok')).textContent).toBe('所有 6 筆紀錄都對得起來');
    expect(must(screen.getAllByTestId('record-row')[1]).textContent).toContain('你暫停了全部。');
  });
});

describe('Permissions', () => {
  it('each permission as a sentence, with its marks, limits, end date and what it has done', async () => {
    await show({ path: '/permissions' });
    const cards = await screen.findAllByTestId('perm');
    const spend = byPerm(cards, SPEND.name);
    expect(within(spend).getByRole('heading', { level: 3 }).textContent).toBe('Your helper may spend up to NT$30 a day on 3 services, until 30 Nov.');
    const marks = within(spend).getByTestId('perm-marks');
    expect(marks.textContent).toContain('Spending limit');
    expect(marks.textContent).toContain('not built yet');
    expect(within(spend).getByText(/Up to NT\$30 a day \(about US\$0\.94\)/)).toBeTruthy();
    const prog = byPerm(cards, PROG.name);
    expect(prog.textContent).toContain('Used once');
    expect(within(prog).getByTestId('perm-records').getAttribute('href')).toBe(`/records?permission=${P_PROG}`);
    expect(screen.getByTestId('perm-declined').textContent).toContain('The program “helper-bot” may use paid services.');
    expect(screen.queryByTestId('perm-ended')).toBeNull(); // ended ones are folded away
    fireEvent.click(screen.getByTestId('perm-ended-toggle'));
    expect(screen.getByTestId('perm-ended').textContent).toContain('Ended 5 Nov');
    expect(document.body.textContent).not.toMatch(/mandate|token|m1_/i);
  });

  it('Stop takes two taps, shows each place turning off one by one, and ends in a plain confirmation', async () => {
    const calls: string[] = [];
    await show({ path: '/permissions', calls });
    const prog = byPerm(await screen.findAllByTestId('perm'), PROG.name);
    fireEvent.click(within(prog).getByTestId('perm-stop')); // tap 1
    const dialog = screen.getByTestId('stop-dialog');
    expect(dialog.textContent).toContain('This program won’t be able to use your allowances any more.');
    fireEvent.click(within(dialog).getByTestId('stop-confirm')); // tap 2
    await waitFor(() => expect(within(dialog).getByTestId('stop-steps')).toBeTruthy());
    const steps = () => [...within(dialog).getByTestId('stop-steps').querySelectorAll('li[data-target]')].map((li) => li.getAttribute('data-state'));
    expect(steps()).toEqual(['waiting', 'waiting']); // nothing is claimed before it is shown
    await waitFor(() => expect(steps()).toEqual(['revoked', 'waiting']), { timeout: 2000 });
    await waitFor(() => expect(steps()).toEqual(['revoked', 'revoked']), { timeout: 2000 });
    expect(within(dialog).getByTestId('stop-done').textContent).toBe('Stopped. A new line is in Records.');
    expect(dialog.textContent).toContain('Using your allowances: Its entry in your allowances is removed');
    expect(calls).toContain(`POST /v1/mandates/${PROG.name}/stop`);
    fireEvent.click(within(dialog).getByTestId('stop-close'));
    await waitFor(() => expect(calls.filter((c) => c === 'GET /v1/mandates?lang=en').length).toBeGreaterThan(1)); // the list is asked again
  });

  it('a place that could not be turned off is shown as failed, with Try again', async () => {
    setWidth(true); // reduced motion in this pretend window: the steps appear at once
    const failed = { stopped: true, was: 'active', id: P_PROG, name: PROG.name, targets: { router: { state: 'failed', reason: 'This could not be removed yet.' }, records: { state: 'revoked', reason: '' } }, complete: false, recorded: true };
    const fixed = { ...failed, was: 'stopped', targets: { router: { state: 'revoked', reason: '' }, records: { state: 'revoked', reason: '' } }, complete: true };
    await show({ path: '/permissions', stop: (n) => (n === 1 ? failed : fixed) });
    const prog = byPerm(await screen.findAllByTestId('perm'), PROG.name);
    fireEvent.click(within(prog).getByTestId('perm-stop'));
    const dialog = screen.getByTestId('stop-dialog');
    fireEvent.click(within(dialog).getByTestId('stop-confirm'));
    const partial = await within(dialog).findByTestId('stop-partial');
    expect(partial.textContent).toContain('Stopped, but one part could not be turned off yet.');
    expect(must(dialog.querySelector('li[data-target="router"]')).getAttribute('data-state')).toBe('failed');
    expect(dialog.textContent).toContain('Using your allowances: Couldn’t be turned off yet');
    fireEvent.click(within(partial).getByTestId('stop-retry'));
    await within(dialog).findByTestId('stop-done');
    expect(must(dialog.querySelector('li[data-target="router"]')).getAttribute('data-state')).toBe('revoked');
  });

  it('cancel changes nothing and focus returns to Stop', async () => {
    const calls: string[] = [];
    await show({ path: '/permissions', calls });
    const spend = byPerm(await screen.findAllByTestId('perm'), SPEND.name);
    const stop = within(spend).getByTestId('perm-stop');
    stop.focus();
    fireEvent.click(stop);
    const dialog = screen.getByTestId('stop-dialog');
    expect(dialog.textContent).toContain('Your helper won’t be able to spend on these services.');
    fireEvent.click(within(dialog).getByRole('button', { name: 'Cancel' }));
    await waitFor(() => expect(document.activeElement).toBe(stop));
    expect(calls.some((c) => c.includes('/stop'))).toBe(false);
  });

  it('a request waiting for a decision is its card, answered right here', async () => {
    await show({ path: '/permissions', cards: [CARDS.grant_permission, CARDS.save_file] });
    const waiting = await screen.findByTestId('perm-waiting');
    expect(within(waiting).getAllByRole('article')).toHaveLength(1);
    expect(within(waiting).getByRole('button', { name: 'Approve' })).toBeTruthy();
  });

  it('desktop: the selected permission’s history beside the list', async () => {
    setWidth(true);
    await show({ path: '/permissions' });
    const history = await screen.findByTestId('perm-history');
    await waitFor(() => expect(history.textContent).toContain('You gave a permission.'));
    expect(within(history).getByRole('link', { name: 'See all of them in Records' }).getAttribute('href')).toBe(`/records?permission=${P_SPEND}`);
  });

  it('none yet, and a home that cannot be reached', async () => {
    await show({ path: '/permissions', perms: [] });
    expect((await screen.findByTestId('perms-empty')).textContent).toContain('No permissions yet');
    cleanup();
    await show({ path: '/permissions', lang: 'zh-TW', broken: ['permissions'] });
    expect((await screen.findByTestId('perms-problem')).textContent).toContain('連不上你的家，所以無法顯示授權。');
  });
});
