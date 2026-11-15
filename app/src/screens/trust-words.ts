// Plain words for records and permissions (docs/spec/02-ux.md sections 1, 4.4, 4.6).
// The home sends facts (an action name, who, under which permission id, a cost in
// minor units, where the content went); these functions turn them into one sentence
// in the person's language. Nothing here shows an id, a hash or an action name: those
// stay behind Details.
import type { Badge, HomeRecord, MoneyInfo, Permission, PermissionLimit, RecordCost } from '../home/types';
import type { Key, Lang, T } from '../i18n';
import { dayKey, DISPLAY_CURRENCY, fmtDate, fmtDateYear, fmtList, fmtLongDate, fmtMoney, fmtNumber, TWD_PER_USD } from '../i18n/format';
import { redact } from '../lib/redact';

export const BADGE_WORD: Record<Badge, Key> = { local: 'bd_device', private: 'bd_private', standard: 'bd_standard' };

export interface ServiceInfo {
  name: string;
  badge: Badge | null;
}

export interface WordsContext {
  t: T;
  lang: Lang;
  /** Permission id -> permission. Null when the home could not list them. */
  permissions: Map<string, Permission> | null;
  services: Map<string, ServiceInfo>;
  money: MoneyInfo | undefined;
}

// ---- money

const MINOR_DIGITS: Record<string, number> = { USDC: 6, JPY: 0, KRW: 0 };

export function amountOf(cost: RecordCost): number {
  return cost.minor / 10 ** (MINOR_DIGITS[cost.currency] ?? 2);
}

function personCurrency(money: MoneyInfo | undefined): { currency: string; rate: number } {
  const rate = Number(money?.rate);
  return { currency: money?.currency ?? DISPLAY_CURRENCY, rate: Number.isFinite(rate) && rate > 0 ? rate : TWD_PER_USD };
}

/** An amount in the person's currency, and the US-dollar figure for Details (rule 7). Converted figures say "about". */
export interface MoneyWords {
  main: string;
  /** The US-dollar figure ("about US$0.94" when converted), or null when the amount is already in US dollars. */
  usd: string | null;
  /** The same figure without "about". */
  usdPlain: string | null;
}

export function moneyWords(amount: number, currency: string, ctx: Pick<WordsContext, 't' | 'lang' | 'money'>): MoneyWords {
  const { t, lang } = ctx;
  const mine = personCurrency(ctx.money);
  const isUsd = currency === 'USD' || currency === 'USDC';
  if (currency === mine.currency) {
    const plain = isUsd ? null : fmtMoney(amount / mine.rate, 'USD', lang);
    return { main: fmtMoney(amount, currency, lang), usd: plain ? t('rc_c_about', plain) : null, usdPlain: plain };
  }
  if (isUsd) {
    const usd = fmtMoney(amount, 'USD', lang);
    return { main: t('rc_c_about', fmtMoney(amount * mine.rate, mine.currency, lang)), usd, usdPlain: usd };
  }
  return { main: fmtMoney(amount, currency, lang), usd: null, usdPlain: null };
}

export function costWords(r: HomeRecord, ctx: WordsContext): MoneyWords {
  if (!r.cost || r.cost.minor <= 0) return { main: ctx.t('rc_c_none'), usd: null, usdPlain: null };
  return moneyWords(amountOf(r.cost), r.cost.currency, ctx);
}

// ---- who, and under which permission

function programName(holder: string): string | null {
  const [kind, ...rest] = holder.split(':');
  const name = rest.join(':');
  return (kind === 'app' || kind === 'device') && name ? redact(name) : null;
}

export function holderWords(holder: string, t: T): string {
  const program = programName(holder);
  return program ? t('pm2_s_program', program) : t('pm2_s_helper');
}

export function subjectOf(r: HomeRecord, ctx: WordsContext): string {
  const { t } = ctx;
  if (r.actor === 'person') return t('rc_s_you');
  if (r.actor === 'system') return t('rc_s_muse');
  const mandate = r.mandate ?? '';
  if (mandate.startsWith('device:')) return t('rc_s_program', redact(mandate.slice('device:'.length)));
  const p = ctx.permissions?.get(mandate);
  const program = p ? programName(p.holder) : null;
  return program ? t('rc_s_program', program) : t('rc_s_helper');
}

/** "Under:" for a record: the permission's sentence, "Asked by you", or what the record names. */
export function underOf(r: HomeRecord, ctx: WordsContext): string {
  const { t } = ctx;
  if (!r.mandate) return r.actor === 'person' ? t('rc_u_you') : t('rc_u_none');
  if (r.mandate.startsWith('device:')) return t('rc_u_device', redact(r.mandate.slice('device:'.length)));
  if (ctx.permissions === null) return t('rc_u_some');
  const p = ctx.permissions.get(r.mandate);
  return p ? permissionSentence(p, ctx, false) : t('rc_u_gone');
}

// ---- where the content went

export function serviceName(id: string, ctx: Pick<WordsContext, 'services'>): string {
  return ctx.services.get(id)?.name ?? redact(id.charAt(0).toUpperCase() + id.slice(1));
}

export interface Where {
  text: string;
  /** Present only when content went to a service: its privacy badge, or null when not known. */
  badge?: Badge | null;
  /** True when the content left the person's devices; null when the record does not say. */
  left: boolean | null;
}

export function whereOf(r: HomeRecord, ctx: WordsContext): Where {
  const { t } = ctx;
  const e = r.egress;
  if (e === undefined) return { text: t('rc_e_unknown'), left: null };
  if (e.startsWith('service:')) {
    const id = e.slice('service:'.length);
    return { text: t('rc_e_service', serviceName(id, ctx)), badge: ctx.services.get(id)?.badge ?? null, left: true };
  }
  if (e.startsWith('host:')) return { text: t('rc_e_program', redact(e.slice('host:'.length))), left: true };
  return { text: t('rc_e_kept'), left: false };
}

// ---- what was done

const CHANGES = /^(files\.write|host\.files\.write|host\.message\.send|key\.store|app\.[a-z_]+|mandate\.[a-z_]+|device\.[a-z_]+)$/;

export function changedSomething(r: HomeRecord): boolean {
  return r.outcome === 'ok' && CHANGES.test(r.action);
}

export function recordSentence(r: HomeRecord, ctx: WordsContext): string {
  const { t } = ctx;
  const who = subjectOf(r, ctx);
  const a = r.action;
  const simple: Record<string, Key> = {
    'home.pause': 'rc_a_pause',
    'home.resume': 'rc_a_resume',
    'card.create': 'rc_a_card_ask',
    'key.store': 'rc_a_service',
    'app.create': 'rc_a_app_add',
    'app.update': 'rc_a_app_change',
    'app.remove': 'rc_a_app_remove',
    'app.rotate': 'rc_a_app_renew',
    'job.queue': 'rc_a_job_add',
    'job.cancel': 'rc_a_job_cancel',
    'skill.run': 'rc_a_task',
    'mandate.issue': 'rc_a_grant',
    'mandate.revoke': 'rc_a_stop',
    'records.export': 'rc_a_export',
    'device.pair': 'rc_a_pair',
    'device.remove': 'rc_a_unpair',
    'host.files.write': 'rc_a_host_save',
    'host.message.send': 'rc_a_host_send',
  };
  let s: string;
  if (a === 'mandate.revoke' && r.actor === 'system') s = t('rc_a_ended');
  else if (a === 'card.answer') s = t(r.outcome === 'declined' ? 'rc_a_card_no' : 'rc_a_card_yes', who);
  else if (a === 'llm.call') s = r.egress?.startsWith('service:') ? t('rc_a_ask_service', who, serviceName(r.egress.slice(8), ctx)) : t('rc_a_ask', who);
  else if (a === 'files.write') s = t(r.outcome === 'failed' ? 'rc_a_save_failed' : 'rc_a_save', who);
  else if (a === 'pay.spend') s = t('rc_a_pay', who, costWords(r, ctx).main);
  else if (simple[a]) s = t(simple[a], who);
  else if (a.startsWith('host.')) s = t('rc_a_host_other', who);
  else s = t('rc_a_other', who);
  const handled = a === 'card.answer' || a === 'files.write';
  if (!handled && r.outcome === 'failed') s = `${s} ${t('rc_o_failed')}`;
  if (!handled && r.outcome === 'declined') s = `${s} ${t('rc_o_declined')}`;
  if (r.outcome === 'undone') s = `${s} ${t('rc_o_undone')}`;
  return s;
}

// ---- days

export function dayHeading(ms: number, now: number, lang: Lang, t: T): string {
  const day = dayKey(ms);
  if (day === dayKey(now)) return t('rc_today', fmtDate(ms, lang));
  if (day === dayKey(now - 86_400_000)) return t('rc_yesterday', fmtDate(ms, lang));
  return fmtLongDate(ms, lang);
}

/** A date, with the year when it is not this year. */
export function dateWords(ms: number, now: number, lang: Lang): string {
  return new Date(ms).getFullYear() === new Date(now).getFullYear() ? fmtDate(ms, lang) : fmtDateYear(ms, lang);
}

// ---- permissions

function folderName(path: string, t: T): string {
  if (path === '*') return t('pm2_p_any_folder');
  const parts = path.split(/[\\/]+/).filter(Boolean);
  return redact(parts[parts.length - 1] ?? path);
}

function limitOf(p: Permission, kind: string, period = 'day'): PermissionLimit | undefined {
  return p.limits.find((l) => l.kind === kind && l.period === period);
}

function limitAmount(l: PermissionLimit): number {
  return l.amount / 10 ** l.decimals;
}

/** What it allows, phrase by phrase; null when it allows something this app has no words for. */
function phrases(p: Permission, ctx: Pick<WordsContext, 't' | 'lang' | 'money'>): string[] | null {
  const { t, lang } = ctx;
  const folders = p.resources.filter((r) => r.kind === 'folder').map((r) => folderName(r.value, t));
  const hosts = p.resources.filter((r) => r.kind === 'host');
  const calendars = p.resources.filter((r) => r.kind === 'calendar').map((r) => (r.value === '*' ? '*' : redact(r.value)));
  const out: string[] = [];
  for (const a of p.actions) {
    if (a === 'llm.call') {
      const n = limitOf(p, 'requests');
      out.push(n ? t('pm2_p_allowance_n', fmtNumber(n.amount, lang)) : t('pm2_p_allowance'));
    } else if (a === 'llm.paid') out.push(t('pm2_p_paid'));
    else if (a === 'files.read' || a === 'files.write') {
      if (!folders.length) return null;
      out.push(t(a === 'files.read' ? 'pm2_p_read' : 'pm2_p_write', fmtList(folders, lang)));
    } else if (a === 'pay.spend') {
      const l = limitOf(p, 'spend');
      if (!l) out.push(t('pm2_p_spend_free', fmtNumber(hosts.length, lang)));
      else {
        const money = moneyWords(limitAmount(l), l.unit, ctx).main;
        out.push(hosts.length === 1 ? t('pm2_p_spend_one', money) : t('pm2_p_spend', money, fmtNumber(hosts.length, lang)));
      }
    } else if (a === 'calendar.read' || a === 'calendar.write') {
      if (!calendars.length) return null;
      out.push(t(a === 'calendar.read' ? 'pm2_p_cal_read' : 'pm2_p_cal_write', fmtList(calendars, lang)));
    } else if (a === 'mail.read') out.push(t('pm2_p_mail_read'));
    else if (a === 'mail.draft') out.push(t('pm2_p_mail_draft'));
    else if (a === 'mail.send') out.push(t('pm2_p_mail_send'));
    else if (a === 'skills.run') out.push(t('pm2_p_tasks'));
    else return null;
  }
  return out.length ? out : null;
}

/** "Your helper may spend up to NT$30 a day on 3 services, until 30 Nov." Falls back to the home's own sentence. */
export function permissionSentence(p: Permission, ctx: Pick<WordsContext, 't' | 'lang' | 'money'>, withEnd = true, now = Date.now()): string {
  const { t, lang } = ctx;
  const list = phrases(p, ctx);
  if (!list) return redact(p.sentence);
  const who = holderWords(p.holder, t);
  const what = fmtList(list, lang);
  const end = Date.parse(p.not_after);
  return withEnd && Number.isFinite(end) ? t('pm2_sentence', who, what, dateWords(end, now, lang)) : t('pm2_sentence_open', who, what);
}

/** The limits line: daily caps (money in the person's currency with the US-dollar figure) and where content may go. */
export function permissionLimits(p: Permission, ctx: Pick<WordsContext, 't' | 'lang' | 'money'>): string[] {
  const { t, lang } = ctx;
  const out: string[] = [];
  for (const l of p.limits) {
    if (l.period !== 'day') continue;
    if (l.kind === 'requests') out.push(t('pm2_l_requests', fmtNumber(l.amount, lang)));
    if (l.kind === 'spend') {
      const m = moneyWords(limitAmount(l), l.unit, ctx);
      out.push(m.usdPlain ? t('pm2_l_spend_usd', m.main, m.usdPlain) : t('pm2_l_spend', m.main));
    }
  }
  if (p.privacy === 'private') out.push(t('pm2_l_private'));
  else if (p.privacy === 'local') out.push(t('pm2_l_local'));
  else if (p.privacy === 'standard') out.push(t('pm2_l_standard'));
  return out;
}

export const TARGET_ORDER = ['skills', 'router', 'payments', 'connectors', 'health', 'records'] as const;
export const TARGET_LABEL: Record<string, Key> = {
  skills: 'pm2_t_skills',
  router: 'pm2_t_router',
  records: 'pm2_t_records',
  payments: 'pm2_t_payments',
  connectors: 'pm2_t_connectors',
  health: 'pm2_t_health',
};

export function orderedTargets<T>(targets: Record<string, T>): [string, T][] {
  const rank = (k: string) => {
    const i = (TARGET_ORDER as readonly string[]).indexOf(k);
    return i < 0 ? TARGET_ORDER.length : i;
  };
  return Object.entries(targets).sort((a, b) => rank(a[0]) - rank(b[0]));
}
