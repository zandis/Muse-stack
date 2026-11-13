// A pretend home for the app's screens (Ask, Chores, Services, Settings, For apps).
import { createAppClient, type AppClient } from '../src/home/app-client';
import type { TransportReply } from '../src/home/client';

export const APP_REPLIES: Record<string, unknown> = {
  'GET /v1/conversations': { conversations: [] },
  'GET /v1/skills': { skills: [], folders: [{ name: 'documents', output: true }] },
  'GET /v1/chores': { chores: [], can_run: true, paused: false },
  'GET /v1/services': { connected: [], add: [], resources: [], subscriptions: [], local_runtime: false, restart_pending: false },
  'GET /v1/settings': {
    settings: {
      daily_limit: { usd: '0.9375', local: '30', approx: false, source: 'default', router_usd: '1.00', in_step: false },
      default_privacy: 'standard',
      language: 'en',
      language_set: false,
      currency: 'TWD',
      rate: '32',
      rate_source: 'fixed setting',
      rate_date: '2026-11-13',
      currencies: ['TWD', 'USD'],
      privacy_choices: ['standard', 'private', 'local'],
      restart_pending: false,
    },
  },
  'GET /v1/memory': { built: false, items: [], conversations: 0 },
  'GET /v1/about': { versions: { muse: '0.1.0' }, catalogue: { checked_on: '2026-11-12', services: 15 }, features: { recovery: false }, endpoint: 'http://127.0.0.1:4000/v1', local_runtime: false },
  'GET /v1/apps/presets': { endpoint: 'http://127.0.0.1:4000/v1', presets: [] },
  'GET /v1/apps': { apps: [] },
};

/** The app client over a transport that answers from `replies` (method and path); 404 for anything else. */
export function pretendAppClient(replies: Record<string, unknown> = APP_REPLIES, calls: string[] = []): AppClient {
  return createAppClient({
    async request(method, path, body): Promise<TransportReply> {
      calls.push(`${method} ${path}${body === undefined ? '' : ` ${JSON.stringify(body)}`}`);
      const r = replies[`${method} ${path}`];
      if (r === undefined) return { status: 404, body: { error: { code: 'not_found', message: 'x' } } };
      return typeof r === 'function' ? { status: 200, body: (r as (b: unknown) => unknown)(body) } : { status: 200, body: r };
    },
  });
}
