// Ask's pure parts: the grey line under an answer, money in the person's currency,
// the three choices in their fixed order, and the sentence for each reason the
// router did not answer. Tested in tests/ask.test.ts.
import type { AskChoices, GreyLine, HandoffApp, LocalAmount } from '../home/types';
import type { Key, T } from '../i18n';
import { fmtDuration, fmtMoney, fmtTime, type Lang } from '../i18n/format';
import { allowedExternal } from '../lib/links';
import { redact } from '../lib/redact';

/** An amount in the person's currency: "NT$1", or "about NT$1" when it was rounded from dollars. */
export function localMoney(a: LocalAmount, currency: string, lang: Lang, t: T): string {
  const text = fmtMoney(a.local, currency, lang);
  return a.approx ? t('about_money', text) : text;
}

/** "Nothing spent." when the amount is zero, else "Spent about NT$1.". */
export function spentText(a: LocalAmount, currency: string, lang: Lang, t: T): string {
  return Number(a.usd) > 0 ? t('gl_spent', localMoney(a, currency, lang, t)) : t('gl_nothing');
}

const str = (v: unknown): string => (typeof v === 'string' ? redact(v) : typeof v === 'number' ? String(v) : '');

function isAmount(v: unknown): v is LocalAmount {
  return typeof v === 'object' && v !== null && typeof (v as LocalAmount).usd === 'string' && typeof (v as LocalAmount).local === 'string';
}

/**
 * The one grey line under an answer: which service answered and what it cost (section 4.2).
 * The home sends a key and its values, never prose, so the line is in the person's language.
 */
export function greyLine(line: GreyLine, cost: LocalAmount | null, o: { currency: string; lang: Lang; t: T; now: number }): string {
  const { t, lang } = o;
  const p = line.params ?? {};
  const service = str(p.service);
  const money = cost ?? (isAmount(p.cost) ? p.cost : null);
  const spent = money ? spentText(money, o.currency, lang, t) : t('gl_nothing');
  switch (line.key) {
    case 'cache':
      return `${t('gl_cache')} ${t('gl_nothing')}`;
    case 'local':
      return `${t('gl_local')} ${spent}`;
    case 'skill_local':
      return `${t('gl_skill_local')} ${t('gl_nothing')}`;
    case 'paid':
      return `${t('gl_by', service)} ${spent}`;
    case 'free_refill': {
      const at = Date.parse(str(p.refill_at));
      if (Number.isFinite(at) && at > o.now) {
        const left = fmtDuration(at - o.now, lang);
        return `${left ? t('gl_allowance_in', service, left) : t('gl_allowance_at', service, fmtTime(at, lang))} ${spent}`;
      }
      return `${t('gl_allowance', service)} ${spent}`;
    }
    case 'free':
      return `${t('gl_by', service)} ${spent}`;
    case 'free_many':
      return `${t('gl_many', str(p.count))} ${spent}`;
    default:
      return spent;
  }
}

export type ChoiceKind = 'wait' | 'handoff' | 'pay';
export const CHOICE_ORDER: readonly ChoiceKind[] = ['wait', 'handoff', 'pay'];

export type OrderedChoice =
  | { kind: 'wait'; until: number | null }
  | { kind: 'handoff'; app: HandoffApp; url: string }
  | { kind: 'pay'; amount: LocalAmount };

/** The person's first own app whose address the opener allows. */
export function openableApp(apps: readonly HandoffApp[] | undefined): { app: HandoffApp; url: string } | null {
  for (const app of apps ?? []) {
    if (app && typeof app.url_web === 'string' && allowedExternal(app.url_web)) return { app, url: app.url_web };
  }
  return null;
}

/** The choices the router named, in the fixed order Wait, Open in my …, Spend; only the real ones. */
export function orderedChoices(choices: AskChoices | undefined): OrderedChoice[] {
  const out: OrderedChoice[] = [];
  if (!choices) return out;
  for (const kind of CHOICE_ORDER) {
    if (kind === 'wait' && choices.wait) {
      const at = Date.parse(choices.wait.until);
      out.push({ kind, until: Number.isFinite(at) ? at : null });
    }
    if (kind === 'handoff' && choices.handoff) {
      const own = openableApp(choices.handoff.apps);
      if (own) out.push({ kind, ...own });
    }
    if (kind === 'pay' && choices.pay && isAmount(choices.pay)) out.push({ kind, amount: choices.pay });
  }
  return out;
}

/** Why the router could not answer now, in one sentence. */
export const REASON_KEY: Record<string, Key> = {
  used_up: 'rs_used_up',
  busy: 'rs_busy',
  yielding: 'rs_yielding',
  paid_needed: 'rs_paid_needed',
  paid_limit: 'rs_paid_limit',
  app_limit: 'rs_app_limit',
  private_no_service: 'rs_private',
  paused: 'rs_paused',
  router_unreachable: 'rs_unreachable',
  refused_key: 'rs_refused_key',
  router_limit: 'rs_router_limit',
  skill_failed: 'rs_skill_failed',
  skill_busy: 'rs_skill_busy',
  empty: 'rs_empty',
  failed: 'rs_failed',
};

export function reasonKey(reason: string | undefined): Key {
  return REASON_KEY[reason ?? 'failed'] ?? 'rs_failed';
}
