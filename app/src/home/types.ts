// Types for the home control API, written by hand from
// docs/spec/interfaces/home-api.v1.yaml and card.schema.json.
// The status snapshot may gain fields; everything the app does not use stays optional.
import type { ActivityItem, MoneyInfo } from './types-app';

export type * from './types-app';
export type * from './types-trust';

export type ErrorCode =
  | 'invalid_request'
  | 'unauthorized'
  | 'forbidden'
  | 'not_found'
  | 'method_not_allowed'
  | 'conflict'
  | 'card_already_answered'
  | 'card_expired'
  | 'length_required'
  | 'payload_too_large'
  | 'internal'
  | 'not_built'
  | 'unavailable';

export const ERROR_CODES: readonly ErrorCode[] = [
  'invalid_request',
  'unauthorized',
  'forbidden',
  'not_found',
  'method_not_allowed',
  'conflict',
  'card_already_answered',
  'card_expired',
  'length_required',
  'payload_too_large',
  'internal',
  'not_built',
  'unavailable',
];

export interface ApiErrorBody {
  error: { code: ErrorCode; message: string; retry_after?: number };
}

/** UTC to the second: 2026-11-13T06:05:00Z */
export type Time = string;

export interface Bilingual {
  en?: string;
  'zh-TW'?: string;
}

// ---- status snapshot

export type ProviderState = 'ok' | 'cooling' | 'exhausted' | 'no_key' | 'disabled' | 'unknown';

export interface Limit {
  window: string; // minute, day, month
  metric: string; // requests, tokens
  limit: number | null;
  remaining: number | null;
  reset_at: Time | null;
  source?: string; // header, estimate, catalog
}

export interface Deployment {
  id: string;
  aliases?: string[];
  state?: string;
}

export interface Provider {
  id: string;
  name: string;
  kind: string; // cloud, local
  tier?: string;
  configured: boolean;
  enabled?: boolean;
  state: ProviderState | string;
  blocked_until?: Time | null;
  limits?: Limit[];
  deployments?: Deployment[];
}

export interface Paid {
  cap_usd: number;
  spent_usd: number;
  held_usd?: number;
  calls?: number;
  resets_at?: Time | null;
}

export interface Suggestion {
  id: string;
  name: string;
  rank?: number;
  minutes_estimate?: number | null;
  steps?: number;
}

export interface Subscription {
  id: string;
  name: string;
  web_url?: string | null;
}

export interface QueueSummary {
  counts?: Record<string, number>;
  waiting?: number;
  next_due_at?: Time | null;
}

export interface Status {
  api_version: '1';
  generated_at: Time;
  paused: boolean;
  default_privacy: string;
  providers: Provider[];
  paid: Paid;
  queue: QueueSummary;
  suggestions: Suggestion[];
  subscriptions: Subscription[];
  /** Added by the app's handler (muse/home/app_api.py); an older home does not send them. */
  money?: MoneyInfo;
  activity?: { running: ActivityItem[]; done_today: ActivityItem[] };
  local_runtime?: boolean;
  restart_pending?: boolean;
}

// ---- cards

export type CardKind = 'save_file' | 'message' | 'spend' | 'grant_permission' | 'oneoff_credit' | 'send_private' | 'install_skill';
export const CARD_KINDS: readonly CardKind[] = ['save_file', 'message', 'spend', 'grant_permission', 'oneoff_credit', 'send_private', 'install_skill'];
export type CardStatus = 'pending' | 'approved' | 'declined' | 'expired';
export type Decision = 'approve' | 'decline';

/** A decimal number as text, so no device rounds money. */
export interface Money {
  amount: string;
  currency: string;
}
export type Cost = { nothing: true } | Money;

interface CardBase {
  id: string;
  title: string;
  thing: string;
  where: string;
  under: { text: string; mandate_id: string | null };
  cost: Cost;
  created_at: Time;
  expires_at: Time;
  status: CardStatus;
  answered_at: Time | null;
  answered_by: string | null;
  always: boolean;
  job_ref: string | null;
  /** The home's own cards: the name of a sentence the app has in every language (card.schema.json). */
  title_key?: string | null;
  params?: Record<string, string | number>;
}

export type Card = CardBase &
  (
    | { kind: 'save_file'; extra: { size_bytes?: number } }
    | { kind: 'message'; extra: { preview: string; to?: string; draft?: boolean } }
    | { kind: 'spend'; cost: Money; extra: { payee: string; limit_left: Money } }
    | { kind: 'grant_permission'; extra: { sentence: string; limits: string[]; ends_at: Time | null } }
    | { kind: 'oneoff_credit'; extra: { service: string; credit_left?: string } }
    | { kind: 'send_private'; extra: { badge: string; services: string[] } }
    | { kind: 'install_skill'; extra: { asks_for: string[]; author: string; sha256?: string } }
  );

export interface CardList {
  cards: Card[];
  counts: Record<string, number>;
}

// ---- jobs

export type JobStatus = 'queued' | 'running' | 'deferred' | 'done' | 'failed' | 'cancelled';

export interface Job {
  id: string;
  created_at: Time;
  kind: 'prompt' | 'file-summary' | 'batch';
  alias: string;
  status: JobStatus;
  attempts: number;
  out_path: string;
  allow_paid: boolean;
  task?: string | null;
  next_attempt_at?: Time | null;
  started_at?: Time | null;
  finished_at?: Time | null;
  provider?: string | null;
  cost_usd?: number | null;
  progress?: number;
}

// ---- apps

export interface App {
  name: string;
  priority: 'interactive' | 'normal' | 'background';
  daily_requests: number | null;
  daily_tokens: number | null;
  allow_paid: boolean;
  default_privacy: string | null;
  note: string | null;
  created_at: Time | null;
}
