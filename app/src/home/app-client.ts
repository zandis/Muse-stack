// The client for the app's screens: Ask, Chores, Services (with the one-tap and
// guided connections), Settings, For apps. Same rules as client.ts: every reply is
// checked for the shape the screen needs, every id is checked before it goes into a
// path, and nothing here ever sees the device token.
import { HomeError, unwrap, type Method, type Transport } from './client';
import type { App } from './types';
import type {
  About,
  AskReply,
  Badge,
  ChoreList,
  ChoreRun,
  Chore,
  ConversationSummary,
  Flow,
  Guide,
  KeyStored,
  NewApp,
  Presets,
  Privacy,
  Restart,
  ServicesReply,
  SettingsData,
  SettingsPatch,
  SettingsSaved,
  TestOutcome,
  Turn,
  Understood,
  VerifyResult,
} from './types-app';
import type { DeclinedRequest, PermissionList, RecordsExport, RecordsPage, StopResult } from './types-trust';

const HEX16 = /^[0-9a-f]{16}$/;
const CHORE_ID = /^[0-9a-f]{8}$/;
const SERVICE_ID = /^[a-z0-9][a-z0-9_-]{0,63}$/;
const APP_NAME = /^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$/;
const PERMISSION_NAME = /^[0-9a-f]{12}$/;
const MANDATE_REF = /^[A-Za-z0-9._:-]{1,128}$/;

function isObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v);
}

function need<T>(ok: boolean, value: T): T {
  if (!ok) throw new HomeError('protocol');
  return value;
}

function id(re: RegExp, value: string): string {
  if (!re.test(value)) throw new HomeError('protocol');
  return value;
}

export type Choice = 'wait' | 'handoff' | 'pay';

export interface AskInput {
  text: string;
  conversation_id?: string | null;
  privacy?: Privacy | undefined;
  attachments?: string[];
}

export interface AppClient {
  ask(input: AskInput): Promise<AskReply>;
  askGet(turnId: string): Promise<AskReply>;
  choose(turnId: string, choice: Choice, o?: { app?: string; raiseToday?: boolean }): Promise<AskReply>;
  /** Ask a cut-short question again with more room. */
  continueAsk(turnId: string): Promise<AskReply>;
  conversations(): Promise<ConversationSummary[]>;
  conversation(id: string): Promise<Turn[]>;
  deleteConversation(id: string): Promise<void>;
  /** The folders the person allowed on the home: the only things Ask can attach. */
  folders(): Promise<string[]>;

  chores(): Promise<ChoreList>;
  understand(sentence: string): Promise<Understood>;
  addChore(understood: Understood): Promise<Chore>;
  setChoreEnabled(id: string, enabled: boolean): Promise<Chore>;
  editChore(id: string, understood: Understood): Promise<Chore>;
  deleteChore(id: string): Promise<void>;
  runChore(id: string): Promise<void>;
  choreHistory(id: string): Promise<ChoreRun[]>;

  services(): Promise<ServicesReply>;
  testService(id: string): Promise<TestOutcome>;
  removeService(id: string): Promise<Restart>;
  guide(id: string): Promise<Guide>;
  /** The key goes straight into the request body; nothing keeps it. */
  storeKey(id: string, key: string): Promise<KeyStored>;
  startOneTap(id: string, sameDevice: boolean): Promise<Flow>;
  flow(stateId: string): Promise<Flow>;
  giveCode(stateId: string, code: string): Promise<Flow>;
  cancelFlow(stateId: string): Promise<void>;
  restartRouter(): Promise<void>;

  settings(): Promise<SettingsData>;
  saveSettings(patch: SettingsPatch): Promise<SettingsSaved>;
  about(): Promise<About>;
  verify(): Promise<VerifyResult>;
  memory(): Promise<{ built: boolean; conversations: number }>;
  forget(): Promise<number>;

  presets(): Promise<Presets>;
  apps(): Promise<App[]>;
  addApp(name: string, priority: App['priority'], o: { dailyRequests?: number | null; allowPaid?: boolean }): Promise<NewApp>;
  removeApp(name: string): Promise<void>;

  /** A page of the home's records, newest first, with whether they check out. The home checks them on every call. */
  records(o?: { before?: number; mandate?: string; limit?: number }): Promise<RecordsPage>;
  exportRecords(redactSummaries: boolean): Promise<RecordsExport>;
  permissions(lang: 'en' | 'zh-TW'): Promise<PermissionList>;
  declined(lang: 'en' | 'zh-TW'): Promise<DeclinedRequest[]>;
  /** Stop one permission everywhere; says how each place it was in force ended up. */
  stopPermission(name: string): Promise<StopResult>;
}

const isTurn = (v: unknown): boolean => isObject(v) && typeof v.id === 'string' && typeof v.status === 'string' && typeof v.question === 'string';
const askReply = (v: Record<string, unknown>): AskReply => need(typeof v.conversation_id === 'string' && isTurn(v.answer), v as unknown as AskReply);
const isChore = (v: unknown): boolean => isObject(v) && typeof v.id === 'string' && typeof v.sentence === 'string';

export function createAppClient(transport: Transport): AppClient {
  const call = async (method: Method, path: string, body?: unknown) => unwrap(await transport.request(method, path, body));
  return {
    async ask(input) {
      const body: Record<string, unknown> = { text: input.text };
      if (input.conversation_id) body.conversation_id = id(HEX16, input.conversation_id);
      if (input.privacy) body.privacy = input.privacy;
      if (input.attachments?.length) body.attachments = input.attachments;
      return askReply(await call('POST', '/v1/ask', body));
    },
    async askGet(turnId) {
      return askReply(await call('GET', `/v1/ask/${id(HEX16, turnId)}`));
    },
    async choose(turnId, choice, o = {}) {
      const body: Record<string, unknown> = { choice };
      if (o.app) body.app = o.app;
      if (o.raiseToday) body.raise_today = true;
      return askReply(await call('POST', `/v1/ask/${id(HEX16, turnId)}/choose`, body));
    },
    async continueAsk(turnId) {
      return askReply(await call('POST', `/v1/ask/${id(HEX16, turnId)}/continue`));
    },
    async conversations() {
      const v = await call('GET', '/v1/conversations');
      return need(Array.isArray(v.conversations), v.conversations as ConversationSummary[]);
    },
    async conversation(cid) {
      const v = await call('GET', `/v1/conversations/${id(HEX16, cid)}`);
      return need(Array.isArray(v.turns) && v.turns.every(isTurn), v.turns as Turn[]);
    },
    async deleteConversation(cid) {
      await call('DELETE', `/v1/conversations/${id(HEX16, cid)}`);
    },
    async folders() {
      // The skills list carries the approved folders (GET /v1/skills `folders`).
      const v = await call('GET', '/v1/skills');
      const raw = Array.isArray(v.folders) ? v.folders : [];
      return raw.map((f) => (isObject(f) && typeof f.name === 'string' ? f.name : typeof f === 'string' ? f : null)).filter((n): n is string => n !== null);
    },

    async chores() {
      const v = await call('GET', '/v1/chores');
      return need(Array.isArray(v.chores) && v.chores.every(isChore), v as unknown as ChoreList);
    },
    async understand(sentence) {
      const v = await call('POST', '/v1/chores/understand', { sentence });
      return need(isObject(v.understood) && typeof v.understood.ok === 'boolean', v.understood as unknown as Understood);
    },
    async addChore(understood) {
      const v = await call('POST', '/v1/chores', { understood });
      return need(isChore(v.chore), v.chore as unknown as Chore);
    },
    async setChoreEnabled(cid, enabled) {
      const v = await call('PATCH', `/v1/chores/${id(CHORE_ID, cid)}`, { enabled });
      return need(isChore(v.chore), v.chore as unknown as Chore);
    },
    async editChore(cid, understood) {
      const v = await call('PATCH', `/v1/chores/${id(CHORE_ID, cid)}`, { understood });
      return need(isChore(v.chore), v.chore as unknown as Chore);
    },
    async deleteChore(cid) {
      await call('DELETE', `/v1/chores/${id(CHORE_ID, cid)}`);
    },
    async runChore(cid) {
      await call('POST', `/v1/chores/${id(CHORE_ID, cid)}/run`);
    },
    async choreHistory(cid) {
      const v = await call('GET', `/v1/chores/${id(CHORE_ID, cid)}/history`);
      return need(Array.isArray(v.runs), v.runs as ChoreRun[]);
    },

    async services() {
      const v = await call('GET', '/v1/services');
      return need(Array.isArray(v.connected) && Array.isArray(v.add), v as unknown as ServicesReply);
    },
    async testService(sid) {
      const v = await call('POST', `/v1/services/${id(SERVICE_ID, sid)}/test`);
      return need(typeof v.result === 'string', v.result as TestOutcome);
    },
    async removeService(sid) {
      const v = await call('DELETE', `/v1/services/${id(SERVICE_ID, sid)}`);
      return need(isObject(v.restart), v.restart as unknown as Restart);
    },
    async guide(sid) {
      const v = await call('POST', `/v1/connect/${id(SERVICE_ID, sid)}`);
      return need(Array.isArray(v.steps) && typeof v.name === 'string', v as unknown as Guide);
    },
    async storeKey(sid, key) {
      const v = await call('POST', `/v1/keys/${id(SERVICE_ID, sid)}`, { key });
      return need(isObject(v.restart), v as unknown as KeyStored);
    },
    async startOneTap(sid, sameDevice) {
      const v = await call('POST', `/v1/connect/${id(SERVICE_ID, sid)}/start`, { same_device: sameDevice });
      return need(typeof v.state_id === 'string' && typeof v.url === 'string', v as unknown as Flow);
    },
    async flow(stateId) {
      const v = await call('GET', `/v1/connect/flows/${id(HEX16, stateId)}`);
      return need(typeof v.status === 'string', v as unknown as Flow);
    },
    async giveCode(stateId, code) {
      const v = await call('POST', `/v1/connect/flows/${id(HEX16, stateId)}/code`, { code });
      return need(typeof v.status === 'string', v as unknown as Flow);
    },
    async cancelFlow(stateId) {
      await call('DELETE', `/v1/connect/flows/${id(HEX16, stateId)}`);
    },
    async restartRouter() {
      await call('POST', '/v1/home/restart-proxy');
    },

    async settings() {
      const v = await call('GET', '/v1/settings');
      return need(isObject(v.settings) && isObject(v.settings.daily_limit), v.settings as unknown as SettingsData);
    },
    async saveSettings(patch) {
      const v = await call('PATCH', '/v1/settings', patch);
      return need(isObject(v.settings) && isObject(v.restart), v as unknown as SettingsSaved);
    },
    async about() {
      const v = await call('GET', '/v1/about');
      return need(isObject(v.features) && isObject(v.versions), v as unknown as About);
    },
    async verify() {
      const v = await call('POST', '/v1/verify');
      return need(typeof v.result === 'string' && Array.isArray(v.lines), v as unknown as VerifyResult);
    },
    async memory() {
      const v = await call('GET', '/v1/memory');
      return { built: v.built === true, conversations: typeof v.conversations === 'number' ? v.conversations : 0 };
    },
    async forget() {
      const v = await call('DELETE', '/v1/memory');
      return typeof v.conversations_removed === 'number' ? v.conversations_removed : 0;
    },

    async presets() {
      const v = await call('GET', '/v1/apps/presets');
      return need(typeof v.endpoint === 'string' && Array.isArray(v.presets), v as unknown as Presets);
    },
    async apps() {
      const v = await call('GET', '/v1/apps');
      return need(Array.isArray(v.apps), v.apps as App[]);
    },
    async addApp(name, priority, o) {
      const body: Record<string, unknown> = { name: id(APP_NAME, name), priority, allow_paid: o.allowPaid === true };
      if (typeof o.dailyRequests === 'number') body.daily_requests = o.dailyRequests;
      const v = await call('POST', '/v1/apps', body);
      return need(isObject(v.app) && typeof v.token === 'string', { app: v.app as unknown as App, token: v.token as string });
    },
    async removeApp(name) {
      await call('DELETE', `/v1/apps/${id(APP_NAME, name)}`);
    },

    async records(o = {}) {
      const q: string[] = [];
      if (o.limit !== undefined) q.push(`limit=${Math.max(1, Math.min(200, Math.floor(o.limit)))}`);
      if (o.before !== undefined) q.push(`before=${need(Number.isInteger(o.before) && o.before >= 0, o.before)}`);
      if (o.mandate !== undefined) q.push(`mandate=${encodeURIComponent(id(MANDATE_REF, o.mandate))}`);
      const v = await call('GET', `/v1/receipts${q.length ? `?${q.join('&')}` : ''}`);
      return need(Array.isArray(v.records) && isObject(v.verify) && typeof v.verify.ok === 'boolean', v as unknown as RecordsPage);
    },
    async exportRecords(redactSummaries) {
      const v = await call('POST', '/v1/receipts/export', { redact_summaries: redactSummaries });
      return need(isObject(v.export) && typeof v.file_name === 'string' && typeof v.count === 'number', v as unknown as RecordsExport);
    },
    async permissions(lang) {
      const v = await call('GET', `/v1/mandates?lang=${lang === 'zh-TW' ? 'zh-TW' : 'en'}`);
      return need(Array.isArray(v.mandates) && isObject(v.enforcement), v as unknown as PermissionList);
    },
    async declined(lang) {
      const v = await call('GET', `/v1/mandates/declined?lang=${lang === 'zh-TW' ? 'zh-TW' : 'en'}`);
      return need(Array.isArray(v.declined), v.declined as DeclinedRequest[]);
    },
    async stopPermission(name) {
      const v = await call('POST', `/v1/mandates/${id(PERMISSION_NAME, name)}/stop`);
      return need(v.stopped === true && isObject(v.targets), v as unknown as StopResult);
    },
  };
}

export type { Badge };
