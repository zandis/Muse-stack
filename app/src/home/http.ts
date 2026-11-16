// Development and tests: same-origin `/v1/...` requests. The Vite server forwards
// them to the home and adds the device token (home-proxy.ts). No token exists here.
import { HomeError, type Method, type Transport, type TransportReply } from './client';

export const REQUEST_TIMEOUT_MS = 15_000;

export function httpTransport(fetchFn: typeof fetch = (...a) => fetch(...a), timeoutMs = REQUEST_TIMEOUT_MS): Transport {
  return {
    async request(method: Method, path: string, body?: unknown): Promise<TransportReply> {
      if (!path.startsWith('/v1/')) throw new HomeError('protocol');
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), timeoutMs);
      let res: Response;
      try {
        res = await fetchFn(path, {
          method,
          headers: body === undefined ? { Accept: 'application/json' } : { Accept: 'application/json', 'Content-Type': 'application/json' },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
          cache: 'no-store',
          credentials: 'omit',
          redirect: 'error',
          referrerPolicy: 'no-referrer',
          signal: ctl.signal,
        });
      } catch {
        throw new HomeError('unreachable');
      } finally {
        clearTimeout(timer);
      }
      let parsed: unknown;
      try {
        const text = await res.text();
        parsed = text ? (JSON.parse(text) as unknown) : null;
      } catch {
        parsed = null;
      }
      return { status: res.status, body: parsed };
    },
  };
}
