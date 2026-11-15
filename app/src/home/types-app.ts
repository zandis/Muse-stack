// Types for the app's screens (Ask, Chores, Services, Settings, For apps), written by
// hand from docs/spec/interfaces/home-api.v1.yaml (the routes of muse/home/app_api.py).
import type { App, Bilingual, Time } from './types';

// ---- money in the person's currency (GET /v1/status `money`, and every amount below)

/** A dollar figure with its local equivalent, both as decimal text. `approx` says the local one was rounded. */
export interface LocalAmount {
  usd: string;
  local: string;
  approx: boolean;
}

export interface DailyLimit extends LocalAmount {
  source?: 'setting' | 'default';
  router_usd?: string;
  in_step?: boolean;
}

export interface MoneyInfo {
  currency: string;
  rate: string;
  rate_source: string;
  rate_date?: string;
  spent_today: LocalAmount;
  daily_limit: DailyLimit;
}

export interface Title {
  key: string;
  params: Record<string, string | number>;
  text: string | null;
}

export interface ActivityItem {
  id: string;
  kind: 'job' | 'chore';
  title: Title;
  status: string;
  at: Time | null;
  ok?: boolean;
  chore_id?: string;
}

// ---- Ask

export type Privacy = 'local' | 'private' | 'standard' | 'public';

export interface HandoffApp {
  id: string;
  name: string;
  url_web: string | null;
  url_app?: string;
}

export interface AskChoices {
  wait?: { until: Time };
  handoff?: { apps: HandoffApp[] };
  pay?: LocalAmount;
}

export interface GreyLine {
  key: string;
  params: Record<string, unknown>;
}

export interface AnswerRoute {
  provider: string | null;
  name: string | null;
  local: boolean;
  cost: LocalAmount;
  cache: boolean;
  refill_hint: Time | null;
  line: GreyLine;
}

export interface Source {
  n: number;
  file: string;
  /** Paragraph numbers; for a PDF source on one page, the paragraphs on that page. */
  paragraphs: number[];
  quote: string;
  label: string;
  /** PDF sources only: the pages the statements rest on. */
  pages?: number[];
}

export type AnswerStatus = 'answered' | 'choices' | 'pending' | 'queued' | 'handed_off' | 'over_limit' | 'failed';

export interface OverLimit {
  over: LocalAmount;
  limit: LocalAmount;
  spent: LocalAmount;
  pay: LocalAmount;
  raise_today: { to: LocalAmount };
}

export interface Turn {
  id: string;
  conversation_id: string;
  created_at: Time;
  question: string;
  privacy: Privacy | null;
  task: string | null;
  attachments: string[];
  status: AnswerStatus;
  text?: string;
  reason?: string;
  route?: AnswerRoute;
  details?: Record<string, unknown>;
  sources?: Source[];
  handoff?: { apps: HandoffApp[] };
  choices?: AskChoices;
  over_limit?: OverLimit;
  job_id?: string;
  run_id?: string;
  until?: Time;
  app?: { id: string; name: string };
  /** The answer stopped at its length limit, or came back empty (the room went on reasoning). */
  cut_short?: boolean;
  can_continue?: boolean;
}

export interface AskReply {
  conversation_id: string;
  answer: Turn;
}

export interface ConversationSummary {
  id: string;
  created_at: Time;
  updated_at: Time;
  turns: number;
  preview: string;
}

// ---- Chores

export interface ChoreQuestion {
  code: string;
  text: string;
}

export interface Understood {
  ok: boolean;
  sentence: string;
  lang: string;
  spec: Record<string, unknown> | null;
  what: string;
  when: string;
  where: string;
  budget: string;
  permission: string;
  questions: ChoreQuestion[];
  assumed: string[];
  notes: string[];
  label?: { spare: boolean };
}

export interface Chore {
  id: string;
  sentence: string;
  enabled: boolean;
  spare_only: boolean;
  next_run: Time | null;
  last_run_at: Time | null;
  last_status: string | null;
  last_summary: string | null;
  when: string;
  budget_text: string;
  lines: { what: string; where: string; budget: string; permission: string; when: string };
  typical_time: string | null;
  paid: boolean;
}

export interface ChoreRun {
  id: string;
  status: string;
  trigger: string;
  started_at: Time | null;
  finished_at: Time | null;
  summary: string | null;
  card_id: string | null;
  requests: number | null;
}

export interface ChoreList {
  chores: Chore[];
  can_run: boolean;
  paused: boolean;
}

// ---- Services

export type Badge = 'local' | 'private' | 'standard';

export type ServiceState = 'ok' | 'busy' | 'used_up' | 'key_refused' | 'off' | 'unknown';

export interface ConnectedService {
  id: string;
  name: string;
  badge: Badge;
  local: boolean;
  left: { remaining: number; limit: number; unit: string | null; estimated: boolean } | null;
  unlimited: boolean;
  refill_at: Time | null;
  estimated: boolean;
  good_for: string[];
  checked_on: string | null;
  state: ServiceState;
  retry_at: Time | null;
  can_test: boolean;
  can_disconnect: boolean;
}

export interface AddService {
  id: string;
  name: string;
  rank: number;
  method: 'one_tap' | 'guided';
  minutes: number | null;
  steps: number | null;
  card: string;
  phone: string;
  gain: { window: string; limit: number; unit: string } | null;
  key_host: string | null;
  facts: { free_per_day: number | null; may_train: string };
}

export interface ResourceEntry {
  id: string;
  kind: string;
  name: Bilingual;
  provider?: string;
  summary?: Bilingual;
}

export interface SubscriptionEntry {
  id: string;
  name: string;
  description?: Bilingual;
  web_url?: string | null;
}

export interface ServicesReply {
  connected: ConnectedService[];
  add: AddService[];
  resources: ResourceEntry[];
  subscriptions: SubscriptionEntry[];
  local_runtime: boolean;
  restart_pending: boolean;
}

export interface GuideStep {
  id: string;
  title: Bilingual;
  detail: Bilingual;
}

export interface Guide {
  provider: string;
  name: string;
  connected: boolean;
  method: string;
  open_url: string | null;
  minutes_estimate: number | null;
  card_required: string;
  phone_required: string;
  steps: GuideStep[];
  key: { name: string; starts_with: string | null };
}

export interface Restart {
  needed: boolean;
  proxy_running: boolean;
  command: string;
  managed: boolean;
}

export interface KeyStored {
  provider: string;
  restart: Restart;
}

export type TestOutcome = 'ok' | 'refused' | 'limited' | 'failed' | 'no_test' | 'no_key';

export interface Flow {
  state_id: string;
  provider: string;
  mode: 'loopback' | 'paste_code';
  status: 'pending' | 'connected' | 'failed';
  reason: string | null;
  expires_at: Time | null;
  url?: string;
  name?: string;
  facts?: { free_per_day: number | null; may_train: string };
  restart?: Restart;
}

// ---- Settings, about, For apps

export interface SettingsData {
  daily_limit: DailyLimit;
  default_privacy: Badge;
  language: string;
  language_set: boolean;
  currency: string;
  rate: string;
  rate_source: string;
  rate_date?: string;
  currencies: string[];
  privacy_choices: Badge[];
  restart_pending: boolean;
  /** `pending` until the first run is finished or skipped on this home. A home that does not send it never starts it. */
  first_run?: 'pending' | 'done' | 'skipped';
}

export interface SettingsPatch {
  daily_limit_local?: string;
  default_privacy?: Badge;
  language?: string;
  currency?: string;
  first_run?: 'done' | 'skipped';
}

export interface SettingsSaved {
  settings: SettingsData;
  restart: Restart;
}

export interface About {
  versions: Record<string, string | null>;
  catalogue: { checked_on: string | null; services: number };
  features: Record<string, boolean>;
  endpoint: string;
  local_runtime: boolean;
}

export interface VerifyResult {
  result: 'ok' | 'problem' | 'unknown';
  lines: { level: string; text: string }[];
}

export interface Preset {
  kind: string;
  title: string;
  priority: App['priority'];
  daily_requests: number | null;
  alias: string;
  steps: string[];
  key_placeholder: string;
  verified: boolean;
  caveat: string | null;
}

export interface Presets {
  endpoint: string;
  presets: Preset[];
}

export interface NewApp {
  app: App;
  /** Shown once. The app never renders it: it goes to the clipboard on an explicit tap, then is dropped. */
  token: string;
}
