// One typed client, two transports (http.ts for development and tests, tauri.ts
// inside the desktop app). Neither transport lets this code see the device token.
import { ERROR_CODES, type ApiErrorBody, type App, type Card, type CardList, type Decision, type ErrorCode, type Job, type Status } from './types';

export type Method = 'GET' | 'POST' | 'PATCH' | 'DELETE';

export interface TransportReply {
  status: number;
  /** Parsed JSON, or null when the reply had no JSON body. */
  body: unknown;
}

export interface Transport {
  /** Rejects with a HomeError of kind `unreachable` when the home cannot be reached at all. */
  request(method: Method, path: string, body?: unknown): Promise<TransportReply>;
}

export type HomeErrorKind = 'unreachable' | 'api' | 'protocol';

export class HomeError extends Error {
  readonly kind: HomeErrorKind;
  readonly code: ErrorCode | null;
  readonly status: number | null;
  /** Seconds, when waiting helps. */
  readonly retryAfter: number | null;

  constructor(kind: HomeErrorKind, o: { code?: ErrorCode | null; status?: number | null; retryAfter?: number | null } = {}) {
    // The message is for developers. Screens show the i18n text chosen from kind and code, never this.
    super(o.code ? `home: ${o.code}` : `home: ${kind}`);
    this.name = 'HomeError';
    this.kind = kind;
    this.code = o.code ?? null;
    this.status = o.status ?? null;
    this.retryAfter = o.retryAfter ?? null;
  }
}

const CARD_ID = /^[0-9a-f]{16}$/;
const STATUS_LIST = /^[a-z]+(?:,[a-z]+)*$/;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function isErrorBody(v: unknown): v is ApiErrorBody {
  if (!isObject(v) || !isObject(v.error)) return false;
  return typeof v.error.code === 'string' && (ERROR_CODES as readonly string[]).includes(v.error.code);
}

/** Turn a reply into the value, or throw the HomeError the screens know how to show. */
export function unwrap(reply: TransportReply): Record<string, unknown> {
  const { status, body } = reply;
  if (status >= 200 && status < 300) {
    if (!isObject(body)) throw new HomeError('protocol', { status });
    return body;
  }
  if (isErrorBody(body)) {
    const retry = body.error.retry_after;
    const retryAfter = typeof retry === 'number' && Number.isFinite(retry) && retry >= 0 ? retry : null;
    // 503 `unavailable` is the home saying it cannot be reached or cannot read its own state.
    const kind: HomeErrorKind = body.error.code === 'unavailable' ? 'unreachable' : 'api';
    throw new HomeError(kind, { code: body.error.code, status, retryAfter });
  }
  // No API error shape: a proxy or gateway answered instead of the home.
  if (status === 502 || status === 503 || status === 504 || status === 500) throw new HomeError('unreachable', { status });
  throw new HomeError('protocol', { status });
}

function need<T>(ok: boolean, value: T): T {
  if (!ok) throw new HomeError('protocol');
  return value;
}

export interface HomeClient {
  status(): Promise<Status>;
  cards(status?: string): Promise<CardList>;
  answerCard(id: string, decision: Decision, always?: boolean): Promise<Card>;
  jobs(status?: string): Promise<Job[]>;
  apps(): Promise<App[]>;
  pause(): Promise<void>;
  resume(): Promise<void>;
}

export function createClient(transport: Transport): HomeClient {
  const call = async (method: Method, path: string, body?: unknown) => unwrap(await transport.request(method, path, body));
  const withStatus = (path: string, status?: string) => {
    if (status === undefined) return path;
    if (!STATUS_LIST.test(status)) throw new HomeError('protocol');
    return `${path}?status=${status}`;
  };
  return {
    async status() {
      const v = await call('GET', '/v1/status');
      return need(v.api_version === '1' && Array.isArray(v.providers) && isObject(v.paid) && typeof v.paused === 'boolean', v as unknown as Status);
    },
    async cards(status) {
      const v = await call('GET', withStatus('/v1/cards', status));
      return need(Array.isArray(v.cards) && isObject(v.counts), v as unknown as CardList);
    },
    async answerCard(id, decision, always = false) {
      if (!CARD_ID.test(id)) throw new HomeError('protocol');
      const body = decision === 'approve' && always ? { decision, always: true } : { decision };
      const v = await call('POST', `/v1/cards/${id}/answer`, body);
      return need(isObject(v.card), v.card as unknown as Card);
    },
    async jobs(status) {
      const v = await call('GET', withStatus('/v1/jobs', status));
      return need(Array.isArray(v.jobs), v.jobs as unknown as Job[]);
    },
    async apps() {
      const v = await call('GET', '/v1/apps');
      return need(Array.isArray(v.apps), v.apps as unknown as App[]);
    },
    async pause() {
      const v = await call('POST', '/v1/pause');
      need(v.paused === true, undefined);
    },
    async resume() {
      const v = await call('POST', '/v1/resume');
      need(v.paused === false, undefined);
    },
  };
}
