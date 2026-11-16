// Dates, times, numbers and money, all through Intl. Times are shown in the
// person's own time zone (the system's, unless a test passes one).
import type { Money } from '../home/types';

export type Lang = 'zh-TW' | 'en';
export const LANGS: readonly Lang[] = ['zh-TW', 'en'];

/** The Intl locale for each language. en-GB gives "13 Nov" and a 24-hour clock, as the specification writes them. */
const LOCALE: Record<Lang, string> = { 'zh-TW': 'zh-TW', en: 'en-GB' };
export const HTML_LANG: Record<Lang, string> = { 'zh-TW': 'zh-Hant-TW', en: 'en' };

/**
 * The currency amounts are shown in. The home reports spending in US dollars only,
 * so the app converts at a fixed, stated rate until the home sends local amounts.
 * Converted figures are always labelled "about".
 */
export const DISPLAY_CURRENCY = 'TWD';
export const TWD_PER_USD = 32;

/** Symbols the specification uses, so NT$ and US$ are never both shown as a bare "$". */
const SYMBOL: Record<string, string> = { TWD: 'NT$', USD: 'US$', JPY: 'JP¥', KRW: '₩' };
const MAX_FRACTION: Record<string, number> = { TWD: 1, USD: 2, USDC: 2, JPY: 0, KRW: 0 };

export function pickLang(preferred: readonly string[]): Lang {
  for (const tag of preferred) {
    const t = tag.toLowerCase();
    if (t.startsWith('zh')) return 'zh-TW';
    if (t.startsWith('en')) return 'en';
  }
  return 'zh-TW'; // the first language of the product
}

export function fmtTime(ms: number, lang: Lang, timeZone?: string): string {
  return new Intl.DateTimeFormat(LOCALE[lang], { hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(timeZone ? { timeZone } : {}) }).format(ms);
}

export function fmtDate(ms: number, lang: Lang, timeZone?: string): string {
  return new Intl.DateTimeFormat(LOCALE[lang], { day: 'numeric', month: 'short', ...(timeZone ? { timeZone } : {}) }).format(ms);
}

export function fmtDateYear(ms: number, lang: Lang, timeZone?: string): string {
  return new Intl.DateTimeFormat(LOCALE[lang], { day: 'numeric', month: 'short', year: 'numeric', ...(timeZone ? { timeZone } : {}) }).format(ms);
}

export function fmtLongDate(ms: number, lang: Lang, timeZone?: string): string {
  return new Intl.DateTimeFormat(LOCALE[lang], { weekday: 'long', day: 'numeric', month: 'long', ...(timeZone ? { timeZone } : {}) }).format(ms);
}

/** YYYY-MM-DD of the local day, for "today" comparisons. */
export function dayKey(ms: number, timeZone?: string): string {
  const parts = new Intl.DateTimeFormat('en-CA', { year: 'numeric', month: '2-digit', day: '2-digit', ...(timeZone ? { timeZone } : {}) }).formatToParts(ms);
  const get = (type: string) => parts.find((p) => p.type === type)?.value ?? '';
  return `${get('year')}-${get('month')}-${get('day')}`;
}

export function fmtNumber(n: number, lang: Lang): string {
  return new Intl.NumberFormat(LOCALE[lang]).format(n);
}

export function fmtMoney(amount: number | string, currency: string, lang: Lang): string {
  const value = typeof amount === 'number' ? amount : Number(amount);
  if (!Number.isFinite(value)) return '';
  const max = MAX_FRACTION[currency] ?? 2;
  const digits = new Intl.NumberFormat(LOCALE[lang], { minimumFractionDigits: currency === 'USD' ? 2 : 0, maximumFractionDigits: max }).format(value);
  const symbol = SYMBOL[currency];
  return symbol ? `${symbol}${digits}` : `${digits} ${currency}`;
}

export function fmtCardMoney(m: Money, lang: Lang): string {
  return fmtMoney(m.amount, m.currency, lang);
}

export function usdToLocal(usd: number): number {
  return Math.round(usd * TWD_PER_USD * 10) / 10;
}

export function localToUsd(amount: number): number {
  return amount / TWD_PER_USD;
}

/** "23 hours", "4 minutes": the time left, rounded down, never less than a minute. Null when under a minute. */
export function fmtDuration(ms: number, lang: Lang): string | null {
  const minutes = Math.floor(ms / 60_000);
  if (minutes < 1) return null;
  const unit = (value: number, u: 'minute' | 'hour' | 'day') => new Intl.NumberFormat(LOCALE[lang], { style: 'unit', unit: u, unitDisplay: 'long' }).format(value);
  if (minutes < 60) return unit(minutes, 'minute');
  const hours = Math.floor(minutes / 60);
  if (hours < 48) return unit(hours, 'hour');
  return unit(Math.floor(hours / 24), 'day');
}

export function fmtBytes(bytes: number, lang: Lang): string {
  const kb = bytes / 1024;
  const pick = kb < 1 ? { v: bytes, u: 'byte' } : kb < 1024 ? { v: kb, u: 'kilobyte' } : { v: kb / 1024, u: 'megabyte' };
  return new Intl.NumberFormat(LOCALE[lang], { style: 'unit', unit: pick.u, unitDisplay: 'short', maximumFractionDigits: pick.v < 10 ? 1 : 0 }).format(pick.v);
}

export function fmtList(items: readonly string[], lang: Lang): string {
  return new Intl.ListFormat(LOCALE[lang], { style: 'short', type: 'conjunction' }).format(items);
}
