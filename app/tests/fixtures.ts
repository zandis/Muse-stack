import type { Card, Job, Provider, Status } from '../src/home/types';

export const NOW = Date.parse('2026-11-13T06:05:00Z'); // 14:05 in Taipei

export function provider(over: Partial<Provider> = {}): Provider {
  return { id: 'gemini', name: 'Gemini', kind: 'cloud', configured: true, enabled: true, state: 'ok', limits: [], deployments: [], ...over };
}

export function status(over: Partial<Status> = {}): Status {
  return {
    api_version: '1',
    generated_at: '2026-11-13T06:05:00Z',
    paused: false,
    default_privacy: 'standard',
    providers: [
      provider({
        limits: [
          { window: 'minute', metric: 'requests', limit: 10, remaining: 0, reset_at: '2026-11-13T06:06:00Z', source: 'header' },
          { window: 'day', metric: 'requests', limit: 100, remaining: 80, reset_at: '2026-11-13T08:00:00Z', source: 'header' },
        ],
        deployments: [{ id: 'a', aliases: ['fast', 'cheap'] }],
      }),
    ],
    paid: { cap_usd: 1, spent_usd: 0, held_usd: 0 },
    queue: {},
    suggestions: [
      { id: 'cerebras', name: 'Cerebras', minutes_estimate: 4 },
      { id: 'cloudflare', name: 'Cloudflare', minutes_estimate: 8 },
    ],
    subscriptions: [{ id: 'chatgpt', name: 'ChatGPT', web_url: 'https://chatgpt.com/' }],
    ...over,
  };
}

const base = {
  where: 'Documents/Muse',
  under: { text: 'Weekly digest chore', mandate_id: null },
  cost: { nothing: true } as const,
  created_at: '2026-11-13T06:00:00Z',
  expires_at: '2026-11-14T06:00:00Z',
  status: 'pending' as const,
  answered_at: null,
  answered_by: null,
  always: false,
  job_ref: null,
};

export const CARDS: Record<Card['kind'], Card> = {
  save_file: { ...base, id: '0000000000000001', kind: 'save_file', title: 'Save a file', thing: 'Weekly digest 13 Nov.md', extra: { size_bytes: 18240 } },
  message: {
    ...base,
    id: '0000000000000002',
    kind: 'message',
    title: 'Send a message',
    thing: 'Reply to Mei about Saturday',
    where: 'LINE',
    extra: { preview: 'Hi Mei, Saturday works for us.\nWe can bring the dessert.\nShall we say 6 pm at your place?', to: 'Mei', draft: false },
  },
  spend: {
    ...base,
    id: '0000000000000003',
    kind: 'spend',
    title: 'Pay for a service',
    thing: 'Transcript of a 42-minute recording',
    where: 'Deepgram',
    cost: { amount: '6', currency: 'TWD' },
    expires_at: '2026-11-13T06:09:10Z',
    extra: { payee: 'Deepgram', limit_left: { amount: '24', currency: 'TWD' } },
  },
  grant_permission: {
    ...base,
    id: '0000000000000004',
    kind: 'grant_permission',
    title: 'Grant a permission',
    thing: 'Weekly digest chore',
    where: 'This computer',
    extra: { sentence: 'Every Monday at 08:00, read these three feeds.', limits: ['Reads 3 feeds you chose', 'Writes only to Documents/Muse'], ends_at: '2027-02-13T00:00:00Z' },
  },
  oneoff_credit: {
    ...base,
    id: '0000000000000005',
    kind: 'oneoff_credit',
    title: 'Use a one-off trial credit',
    thing: 'Summarise a 180-page contract',
    where: 'Cohere',
    extra: { service: 'Cohere', credit_left: 'about 400 of 1,000 trial calls' },
  },
  send_private: {
    ...base,
    id: '0000000000000006',
    kind: 'send_private',
    title: 'Send something private to a service',
    thing: 'Lab results 2026-10.pdf',
    where: 'A service that does not keep what you send',
    extra: { badge: 'Private', services: ['Groq', 'AI on this computer'] },
  },
  install_skill: {
    ...base,
    id: '0000000000000007',
    kind: 'install_skill',
    title: 'Install a skill',
    thing: 'Feed reader',
    where: 'This computer',
    extra: { asks_for: ['Read web pages you list', 'Save files to Documents/Muse'], author: 'Muse', sha256: '9f2c4e1ab7d05c6638e0f1a4d2b97c5e8a3f60b14d7e92c5a8b1f3e6d4c7a091' },
  },
};

export function job(over: Partial<Job> = {}): Job {
  return { id: 'aaaaaaaaaaaa', created_at: '2026-11-13T05:00:00Z', kind: 'prompt', alias: 'auto', status: 'queued', attempts: 0, out_path: '/x', allow_paid: false, ...over };
}
