import { describe, expect, it, vi } from 'vitest';
import { createClient, HomeError, unwrap, type Transport } from '../src/home/client';
import { httpTransport } from '../src/home/http';
import { isTauri, tauriTransport } from '../src/home/tauri';
import { CARDS, status } from './fixtures';

function fake(reply: { status: number; body: unknown }) {
  const request = vi.fn<Transport['request']>(async () => reply);
  return { transport: { request } as Transport, request };
}

async function caught(p: Promise<unknown>): Promise<HomeError> {
  try {
    await p;
  } catch (e) {
    if (e instanceof HomeError) return e;
    throw e;
  }
  throw new Error('expected a HomeError');
}

describe('unwrap', () => {
  it('returns the object of a 2xx reply', () => {
    expect(unwrap({ status: 200, body: { a: 1 } })).toEqual({ a: 1 });
  });
  it('maps the API error shape, with retry_after', () => {
    const e = (() => {
      try {
        unwrap({ status: 409, body: { error: { code: 'card_already_answered', message: 'x' } } });
      } catch (err) {
        return err as HomeError;
      }
      return null;
    })();
    expect(e?.kind).toBe('api');
    expect(e?.code).toBe('card_already_answered');
    expect(e?.status).toBe(409);
  });
  it('treats 503 unavailable as unreachable and keeps retry_after', async () => {
    const e = await caught(Promise.resolve().then(() => unwrap({ status: 503, body: { error: { code: 'unavailable', message: 'x', retry_after: 5 } } })));
    expect(e.kind).toBe('unreachable');
    expect(e.retryAfter).toBe(5);
  });
  it('treats a gateway error without the API shape as unreachable', async () => {
    for (const s of [500, 502, 503, 504]) expect((await caught(Promise.resolve().then(() => unwrap({ status: s, body: null })))).kind).toBe('unreachable');
  });
  it('never puts the server message into the error text', async () => {
    const e = await caught(Promise.resolve().then(() => unwrap({ status: 400, body: { error: { code: 'invalid_request', message: 'secret detail' } } })));
    expect(e.message).not.toContain('secret');
  });
  it('rejects a 2xx that is not an object, and an unknown error code', async () => {
    expect((await caught(Promise.resolve().then(() => unwrap({ status: 200, body: [] })))).kind).toBe('protocol');
    expect((await caught(Promise.resolve().then(() => unwrap({ status: 418, body: { error: { code: 'teapot', message: '' } } })))).kind).toBe('protocol');
  });
});

describe('client', () => {
  it('reads the status snapshot', async () => {
    const { transport, request } = fake({ status: 200, body: status() });
    expect((await createClient(transport).status()).api_version).toBe('1');
    expect(request).toHaveBeenCalledWith('GET', '/v1/status', undefined);
  });
  it('refuses a snapshot from another API version', async () => {
    const { transport } = fake({ status: 200, body: { ...status(), api_version: '2' } });
    expect((await caught(createClient(transport).status())).kind).toBe('protocol');
  });
  it('asks for waiting cards', async () => {
    const { transport, request } = fake({ status: 200, body: { cards: [CARDS.save_file], counts: { pending: 1 } } });
    expect((await createClient(transport).cards('pending')).cards).toHaveLength(1);
    expect(request).toHaveBeenCalledWith('GET', '/v1/cards?status=pending', undefined);
  });
  it('refuses a status filter that is not a plain list', async () => {
    const { transport, request } = fake({ status: 200, body: { cards: [], counts: {} } });
    expect((await caught(createClient(transport).cards('pending&x=1'))).kind).toBe('protocol');
    expect(request).not.toHaveBeenCalled();
  });
  it('answers a card, sending always only with approve', async () => {
    const { transport, request } = fake({ status: 200, body: { card: { ...CARDS.save_file, status: 'approved' } } });
    const c = createClient(transport);
    await c.answerCard('0000000000000001', 'approve', true);
    expect(request).toHaveBeenLastCalledWith('POST', '/v1/cards/0000000000000001/answer', { decision: 'approve', always: true });
    await c.answerCard('0000000000000001', 'decline', true);
    expect(request).toHaveBeenLastCalledWith('POST', '/v1/cards/0000000000000001/answer', { decision: 'decline' });
  });
  it('never builds a path from a card id that is not 16 hex digits', async () => {
    const { transport, request } = fake({ status: 200, body: {} });
    expect((await caught(createClient(transport).answerCard('../apps', 'approve'))).kind).toBe('protocol');
    expect(request).not.toHaveBeenCalled();
  });
  it('pauses and resumes', async () => {
    const p = fake({ status: 200, body: { paused: true } });
    await createClient(p.transport).pause();
    expect(p.request).toHaveBeenCalledWith('POST', '/v1/pause', undefined);
    const r = fake({ status: 200, body: { paused: false, was_paused: true } });
    await createClient(r.transport).resume();
    expect(r.request).toHaveBeenCalledWith('POST', '/v1/resume', undefined);
  });
  it('lists jobs and apps', async () => {
    expect(await createClient(fake({ status: 200, body: { jobs: [] } }).transport).jobs()).toEqual([]);
    expect(await createClient(fake({ status: 200, body: { apps: [] } }).transport).apps()).toEqual([]);
  });
});

describe('http transport', () => {
  it('sends a same-origin request with no credentials and no token', async () => {
    const fetchFn = vi.fn(async () => new Response(JSON.stringify({ ok: 1 }), { status: 200 }));
    const reply = await httpTransport(fetchFn as unknown as typeof fetch).request('POST', '/v1/cards/x/answer', { decision: 'approve' });
    expect(reply).toEqual({ status: 200, body: { ok: 1 } });
    const [url, init] = fetchFn.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/v1/cards/x/answer');
    expect(init.credentials).toBe('omit');
    expect(init.redirect).toBe('error');
    expect(JSON.stringify(init.headers)).not.toMatch(/authorization/i);
    expect(init.body).toBe('{"decision":"approve"}');
  });
  it('turns a network failure into unreachable', async () => {
    const t = httpTransport((async () => {
      throw new TypeError('failed');
    }) as unknown as typeof fetch);
    expect((await caught(t.request('GET', '/v1/status'))).kind).toBe('unreachable');
  });
  it('gives a null body for a reply that is not JSON', async () => {
    const t = httpTransport((async () => new Response('<html>bad gateway</html>', { status: 502 })) as unknown as typeof fetch);
    expect(await t.request('GET', '/v1/status')).toEqual({ status: 502, body: null });
  });
  it('only calls paths under /v1/', async () => {
    const fetchFn = vi.fn();
    expect((await caught(httpTransport(fetchFn as unknown as typeof fetch).request('GET', 'https://example.com/'))).kind).toBe('protocol');
    expect(fetchFn).not.toHaveBeenCalled();
  });
});

describe('tauri transport', () => {
  it('invokes home_request and passes the reply through', async () => {
    const invoke = vi.fn(async () => ({ status: 200, body: { paused: true } }));
    const reply = await tauriTransport(invoke).request('POST', '/v1/pause');
    expect(invoke).toHaveBeenCalledWith('home_request', { method: 'POST', path: '/v1/pause', body: null });
    expect(reply).toEqual({ status: 200, body: { paused: true } });
  });
  it('maps a rejected command to unreachable, and a refused request to protocol', async () => {
    expect((await caught(tauriTransport(async () => Promise.reject('unreachable')).request('GET', '/v1/status'))).kind).toBe('unreachable');
    expect((await caught(tauriTransport(async () => Promise.reject('no_token')).request('GET', '/v1/status'))).kind).toBe('unreachable');
    expect((await caught(tauriTransport(async () => Promise.reject('invalid_request')).request('GET', '/v1/status'))).kind).toBe('protocol');
    expect((await caught(tauriTransport(async () => 'nonsense').request('GET', '/v1/status'))).kind).toBe('protocol');
  });
  it('knows when it is inside the desktop app', () => {
    expect(isTauri({})).toBe(false);
    expect(isTauri({ __TAURI_INTERNALS__: {} })).toBe(true);
  });
});
