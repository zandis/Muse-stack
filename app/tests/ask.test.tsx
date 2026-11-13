// Ask's pure parts (grey line, money, choice order), the app client, and the
// Markdown renderer against hostile answers.
import { cleanup, fireEvent, render } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createAppClient } from '../src/home/app-client';
import { HomeError, type TransportReply } from '../src/home/client';
import type { LocalAmount } from '../src/home/types';
import { translate, type Key } from '../src/i18n';
import type { Lang } from '../src/i18n/format';
import { allowCatalogueHosts, allowedExternal } from '../src/lib/links';
import { Markdown, parseInline, parseMarkdown } from '../src/lib/markdown';
import { CHOICE_ORDER, greyLine, localMoney, orderedChoices, reasonKey, spentText } from '../src/state/ask';
import { pretendAppClient } from './app-fixtures';

const T = (lang: Lang) => (key: Key, ...args: (string | number)[]) => translate(lang, key, ...args);
const NOW = Date.parse('2026-11-13T06:05:00Z');
const ZERO: LocalAmount = { usd: '0.00', local: '0', approx: false };
const FOUR_CENTS: LocalAmount = { usd: '0.04', local: '1', approx: true };

afterEach(cleanup);

describe('money in the person’s currency', () => {
  it('says "about" only when the local figure was rounded', () => {
    expect(localMoney(FOUR_CENTS, 'TWD', 'en', T('en'))).toBe('about NT$1');
    expect(localMoney({ usd: '0.9375', local: '30', approx: false }, 'TWD', 'en', T('en'))).toBe('NT$30');
    expect(localMoney(FOUR_CENTS, 'TWD', 'zh-TW', T('zh-TW'))).toBe('約 NT$1');
    expect(localMoney({ usd: '1.00', local: '1.00', approx: false }, 'USD', 'en', T('en'))).toBe('US$1.00');
  });
  it('says "Nothing spent" for zero, in both languages', () => {
    expect(spentText(ZERO, 'TWD', 'en', T('en'))).toBe('Nothing spent.');
    expect(spentText(ZERO, 'TWD', 'zh-TW', T('zh-TW'))).toBe('沒有花錢。');
    expect(spentText(FOUR_CENTS, 'TWD', 'en', T('en'))).toBe('Spent about NT$1.');
  });
});

describe('the grey line', () => {
  const o = (lang: Lang) => ({ currency: 'TWD', lang, t: T(lang), now: NOW });
  it('names the service and what it cost, from a key and its values (never prose from the home)', () => {
    expect(greyLine({ key: 'free_refill', params: { service: 'Groq', refill_at: '2026-11-13T09:05:00Z' } }, ZERO, o('en'))).toBe('Used your Groq allowance (refills in 3 hours). Nothing spent.');
    expect(greyLine({ key: 'free_refill', params: { service: 'Groq', refill_at: '2026-11-13T09:05:00Z' } }, ZERO, o('zh-TW'))).toBe('用了你的 Groq 額度（3 小時後補滿）。沒有花錢。');
    expect(greyLine({ key: 'free', params: { service: 'OpenRouter' } }, ZERO, o('en'))).toBe('Answered by OpenRouter. Nothing spent.');
    expect(greyLine({ key: 'free', params: { service: 'OpenRouter' } }, ZERO, o('zh-TW'))).toBe('由 OpenRouter 回答。沒有花錢。');
    expect(greyLine({ key: 'paid', params: { service: 'DeepSeek' } }, FOUR_CENTS, o('en'))).toBe('Answered by DeepSeek. Spent about NT$1.');
    expect(greyLine({ key: 'local', params: {} }, ZERO, o('en'))).toBe('Answered by the AI on this computer. Nothing spent.');
    expect(greyLine({ key: 'cache', params: {} }, ZERO, o('zh-TW'))).toBe('用之前的答案回答。沒有花錢。');
    expect(greyLine({ key: 'free_many', params: { count: 2 } }, ZERO, o('en'))).toBe('Answered with 2 services. Nothing spent.');
    expect(greyLine({ key: 'something_new', params: {} }, ZERO, o('en'))).toBe('Nothing spent.');
  });
  it('hides anything key-shaped that slipped into a value', () => {
    expect(greyLine({ key: 'free', params: { service: 'sk-abcdefghijklmnopqrstuvwxyz' } }, ZERO, o('en'))).not.toContain('sk-abc');
  });
  it('a refill time already past is not shown as a countdown', () => {
    expect(greyLine({ key: 'free_refill', params: { service: 'Groq', refill_at: '2026-11-12T09:05:00Z' } }, ZERO, o('en'))).toBe('Used your Groq allowance. Nothing spent.');
  });
});

describe('the three choices', () => {
  const apps = [
    { id: 'evil', name: 'Evil', url_web: 'https://evil.example/' },
    { id: 'chatgpt', name: 'ChatGPT', url_web: 'https://chatgpt.com/?q=hi' },
  ];
  it('come in the fixed order Wait, Open in my …, Spend, whatever order the home sent', () => {
    expect(CHOICE_ORDER).toEqual(['wait', 'handoff', 'pay']);
    const got = orderedChoices({ pay: FOUR_CENTS, handoff: { apps }, wait: { until: '2026-11-13T08:00:00Z' } });
    expect(got.map((c) => c.kind)).toEqual(['wait', 'handoff', 'pay']);
  });
  it('only the real ones: no app the opener allows means no "Open in my …"', () => {
    expect(orderedChoices({ handoff: { apps: apps.slice(0, 1) }, pay: FOUR_CENTS }).map((c) => c.kind)).toEqual(['pay']);
    expect(orderedChoices({ wait: { until: 'nonsense' } })).toEqual([{ kind: 'wait', until: null }]);
    expect(orderedChoices(undefined)).toEqual([]);
    const own = orderedChoices({ handoff: { apps } })[0];
    expect(own?.kind === 'handoff' && own.app.id).toBe('chatgpt');
  });
  it('every reason has a sentence', () => {
    expect(reasonKey('private_no_service')).toBe('rs_private');
    expect(reasonKey('never-heard-of-it')).toBe('rs_failed');
    expect(reasonKey(undefined)).toBe('rs_failed');
  });
});

describe('the opener allow-list', () => {
  it('takes service key-page hosts from the home, plain DNS names only, https only', () => {
    expect(allowedExternal('https://cloud.cerebras.ai/keys')).toBeNull();
    allowCatalogueHosts(['cloud.cerebras.ai', 'evil.example/x', 'javascript:alert(1)', null, '127.0.0.1']);
    expect(allowedExternal('https://cloud.cerebras.ai/keys')?.hostname).toBe('cloud.cerebras.ai');
    expect(allowedExternal('http://cloud.cerebras.ai/keys')).toBeNull();
    expect(allowedExternal('https://127.0.0.1/')).toBeNull();
  });
});

describe('the app client', () => {
  it('checks ids before they go into a path', async () => {
    const calls: string[] = [];
    const c = pretendAppClient({}, calls);
    await expect(c.askGet('../status')).rejects.toBeInstanceOf(HomeError);
    await expect(c.deleteChore('x/../y')).rejects.toBeInstanceOf(HomeError);
    await expect(c.testService('Groq!')).rejects.toBeInstanceOf(HomeError);
    expect(calls).toEqual([]);
  });
  it('sends a question with only the fields it has, and a key only in the body', async () => {
    const sent: { method: string; path: string; body: unknown }[] = [];
    const reply = (body: unknown): TransportReply => ({ status: 200, body });
    const c = createAppClient({
      async request(method, path, body) {
        sent.push({ method, path, body });
        if (path === '/v1/ask') return reply({ conversation_id: '0123456789abcdef', answer: { id: 'aaaaaaaaaaaaaaaa', status: 'answered', question: 'hi' } });
        return reply({ restart: { needed: true, proxy_running: true, command: 'muse restart', managed: false }, provider: 'groq' });
      },
    });
    await c.ask({ text: 'hi', privacy: 'private', attachments: ['documents'] });
    await c.storeKey('groq', 'gsk_secret');
    expect(sent[0]).toEqual({ method: 'POST', path: '/v1/ask', body: { text: 'hi', privacy: 'private', attachments: ['documents'] } });
    expect(sent[1]?.path).toBe('/v1/keys/groq');
    expect(sent[1]?.path).not.toContain('gsk_');
  });
  it('turns a reply of the wrong shape into a protocol error', async () => {
    const c = pretendAppClient({ 'GET /v1/chores': { nope: true } });
    await expect(c.chores()).rejects.toMatchObject({ kind: 'protocol' });
  });
});

describe('Markdown', () => {
  it('parses headings, lists, tables, code and inline marks', () => {
    const b = parseMarkdown('## Plan\n\n1. **one**\n2. two\n\n| a | b |\n| --- | --- |\n| 1 | 2 |\n\n```\ncode <b>\n```\n\n- x\n- y');
    expect(b.map((x) => x.t)).toEqual(['h', 'ol', 'table', 'code', 'ul']);
    expect(parseInline('a `c` *e* [l](https://claude.ai/)').map((x) => x.t)).toEqual(['text', 'code', 'text', 'em', 'text', 'link']);
  });

  it('a hostile answer runs nothing and loads nothing from another origin', () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null);
    const hostile = [
      '<script>window.__pwned = 1</script>',
      '<img src="https://evil.example/x.png" onerror="window.__pwned=2">',
      '[click me](javascript:window.__pwned=3)',
      '[data](data:text/html,<script>alert(1)</script>)',
      '![tracker](https://evil.example/pixel.gif)',
      '<iframe src="https://evil.example/"></iframe>',
      '[ok](https://chatgpt.com/)',
    ].join('\n\n');
    const { container } = render(<Markdown text={hostile} />);
    // Nothing became an element that runs or loads: all of it is text.
    for (const tag of ['script', 'img', 'iframe', 'a', 'object', 'embed', 'link', 'style']) expect(container.querySelectorAll(tag)).toHaveLength(0);
    expect(container.textContent).toContain('<script>window.__pwned = 1</script>');
    expect(container.textContent).toContain('[tracker]');
    expect((window as unknown as { __pwned?: number }).__pwned).toBeUndefined();
    // Only the allowed link is a button, and it goes through the opener.
    const buttons = container.querySelectorAll('button.mdlink');
    expect(buttons).toHaveLength(1);
    fireEvent.click(buttons[0] as Element);
    expect(open).toHaveBeenCalledWith('https://chatgpt.com/', '_blank', 'noopener,noreferrer');
    for (const el of container.querySelectorAll('*')) for (const a of el.getAttributeNames()) expect(a.startsWith('on')).toBe(false);
    open.mockRestore();
  });

  it('survives very large and deeply nested input', () => {
    expect(parseInline('*'.repeat(5000) + 'x').length).toBeGreaterThan(0);
    expect(parseMarkdown('a\n'.repeat(100_000)).length).toBe(1);
  });
});
