import { describe, expect, it } from 'vitest';
import { DICTS, fill, translate } from '../src/i18n';
import { en } from '../src/i18n/en';
import { dayKey, fmtBytes, fmtDate, fmtDuration, fmtList, fmtLongDate, fmtMoney, fmtTime, localToUsd, pickLang, usdToLocal } from '../src/i18n/format';
import { zhTW } from '../src/i18n/zh-TW';
import linkHosts from '../src/lib/link-hosts.json';
import { ALLOWED_HOSTS, allowedExternal, openExternal } from '../src/lib/links';
import { fingerprint, HIDDEN, redact } from '../src/lib/redact';
import { NOW } from './fixtures';

const TPE = 'Asia/Taipei';
const placeholders = (s: string) => [...s.matchAll(/\{(\d+)\}/g)].map((m) => m[1]).sort().join(',');

describe('dictionaries', () => {
  const keys = Object.keys(en) as (keyof typeof en)[];
  it('have every key in both languages, with text', () => {
    expect(Object.keys(zhTW).sort()).toEqual([...keys].sort());
    for (const k of keys) {
      expect(en[k].trim(), `en ${k}`).not.toBe('');
      expect(zhTW[k].trim(), `zh-TW ${k}`).not.toBe('');
    }
  });
  it('use the same placeholders in both languages', () => {
    for (const k of keys) expect(placeholders(zhTW[k]), k).toBe(placeholders(en[k]));
  });
  it('really are translated (only names stay the same)', () => {
    const same = keys.filter((k) => en[k] === zhTW[k]);
    expect(same.sort()).toEqual(['brand', 'doc_title', 'st_lang_en', 'st_lang_zh']);
  });
  it('use the plain words of UX section 1, not the technical ones', () => {
    // Words the table rules out on main screens. ("Key" is allowed only for For apps.)
    const banned = /\b(agent|bot|provider|API|quota|rate limit|resets?|mandate|scope|receipt|log|VM|server|token|model|end-to-end)\b/i;
    // Details (dt_ keys) is where model names and tokens may be named (section 1, rule 2).
    const main = keys.filter((k) => !k.startsWith('dt_'));
    for (const k of main) expect(en[k], k).not.toMatch(banned);
    const bannedZh = /(代理程式|機器人|供應商|配額|重置|伺服器|模型|權杖)/;
    for (const k of main) expect(zhTW[k], k).not.toMatch(bannedZh);
  });
  it('fill placeholders', () => {
    expect(fill('{0} of {1}', ['a', 2])).toBe('a of 2');
    expect(fill('{0} {1}', ['a'])).toBe('a {1}');
    expect(translate('en', 'g_next', '16:00', 'Gemini')).toBe('Next refill 16:00 (Gemini)');
    expect(translate('zh-TW', 'g_refills', '16:00')).toBe('16:00 補滿');
    expect(DICTS.en).toBe(en);
  });
});

describe('formatting', () => {
  it('picks the language from the system list', () => {
    expect(pickLang(['zh-TW', 'en'])).toBe('zh-TW');
    expect(pickLang(['zh-Hant-HK'])).toBe('zh-TW');
    expect(pickLang(['en-US'])).toBe('en');
    expect(pickLang(['fr-FR', 'en-GB'])).toBe('en');
    expect(pickLang(['ja-JP'])).toBe('zh-TW');
    expect(pickLang([])).toBe('zh-TW');
  });
  it('shows times on a 24-hour clock in the given time zone', () => {
    expect(fmtTime(NOW, 'en', TPE)).toBe('14:05');
    expect(fmtTime(NOW, 'zh-TW', TPE)).toBe('14:05');
    expect(fmtTime(NOW, 'en', 'Europe/London')).toBe('06:05');
    expect(fmtTime(Date.parse('2026-11-13T16:00:00Z'), 'en', TPE)).toBe('00:00');
  });
  it('shows dates the way each language writes them', () => {
    expect(fmtDate(NOW, 'en', TPE)).toBe('13 Nov');
    expect(fmtDate(NOW, 'zh-TW', TPE)).toBe('11月13日');
    expect(fmtLongDate(NOW, 'en', TPE)).toBe('Friday 13 November');
    expect(fmtLongDate(NOW, 'zh-TW', TPE)).toContain('星期五');
  });
  it('knows which local day a moment falls on', () => {
    expect(dayKey(Date.parse('2026-11-13T15:59:00Z'), TPE)).toBe('2026-11-13');
    expect(dayKey(Date.parse('2026-11-13T16:00:00Z'), TPE)).toBe('2026-11-14');
  });
  it('writes money as NT$ and US$, never a bare $', () => {
    expect(fmtMoney(30, 'TWD', 'en')).toBe('NT$30');
    expect(fmtMoney('1.3', 'TWD', 'zh-TW')).toBe('NT$1.3');
    expect(fmtMoney(1234.56, 'TWD', 'en')).toBe('NT$1,234.6');
    expect(fmtMoney(0.03, 'USD', 'zh-TW')).toBe('US$0.03');
    expect(fmtMoney(1, 'USD', 'en')).toBe('US$1.00');
    expect(fmtMoney('2.5', 'USDC', 'en')).toBe('2.5 USDC');
    expect(fmtMoney('abc', 'TWD', 'en')).toBe('');
  });
  it('converts at the stated rate', () => {
    expect(usdToLocal(1)).toBe(32);
    expect(usdToLocal(0.03)).toBe(1);
    expect(localToUsd(6)).toBeCloseTo(0.1875);
  });
  it('writes how long is left', () => {
    expect(fmtDuration(23.9 * 3600_000, 'en')).toBe('23 hours');
    expect(fmtDuration(4.5 * 60_000, 'en')).toBe('4 minutes');
    expect(fmtDuration(60_000, 'en')).toBe('1 minute');
    expect(fmtDuration(3 * 24 * 3600_000, 'en')).toBe('3 days');
    expect(fmtDuration(24 * 3600_000, 'zh-TW')).toBe('24 小時');
    expect(fmtDuration(30_000, 'en')).toBeNull();
    expect(fmtDuration(-5, 'en')).toBeNull();
  });
  it('writes sizes and lists', () => {
    expect(fmtBytes(18240, 'en')).toBe('18 kB');
    expect(fmtBytes(512, 'en')).toBe('512 byte');
    expect(fmtBytes(5 * 1024 * 1024, 'en')).toBe('5 MB');
    expect(fmtList(['Groq', 'AI on this computer'], 'en')).toBe('Groq and AI on this computer');
    expect(fmtList(['A', 'B'], 'zh-TW')).toBe('A和B');
  });
});

describe('redaction', () => {
  it('hides anything shaped like a key or token', () => {
    for (const secret of [
      'sk-abcdefghijklmnop1234',
      'sk-proj-Ab12Cd34Ef56Gh78Ij90',
      'gsk_abcdefghijklmnopqrstuvwx',
      'ghp_abcdefghijklmnopqrstuvwxyz0123456789',
      'AIzaSyA1234567890abcdefghijklmnopqrstuv',
      'xoxb-123456789012-abcdefghijkl',
      'Bearer abcdefghijklmnopqrstuvwxyz',
      'eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dozjgNryP4J3jVmNHl0w5N',
      'q9Zk3mXv7Lp2Rt8Wy4Bn6Cd1Fg5Hj0Ks9Zk3mXv7Lp2Rt8Wy',
      'fake-home-token-fake-home-token-0123456789abcdef',
    ]) {
      const out = redact(`before ${secret} after`);
      expect(out, secret).not.toContain(secret);
      expect(out).toContain(HIDDEN);
      expect(out.startsWith('before ')).toBe(true);
    }
  });
  it('leaves ordinary text, names, paths and amounts alone', () => {
    for (const text of [
      'Weekly digest 13 Nov.md',
      'Documents/Muse/reports/2026/quarterly-summary-final-version-3.md',
      'Transcript of a 42-minute recording',
      'Paid services, up to NT$30 a day',
      '每週摘要 11 月 13 日.md',
      'about 400 of 1,000 trial calls',
      'https://chatgpt.com/',
      'a-very-long-hyphenated-file-name-without-any-digits-in-it-at-all',
    ]) {
      expect(redact(text)).toBe(text);
    }
  });
  it('accepts only a 64-digit hex fingerprint', () => {
    const fp = '9f2c4e1ab7d05c6638e0f1a4d2b97c5e8a3f60b14d7e92c5a8b1f3e6d4c7a091';
    expect(fingerprint(fp)).toBe(fp);
    expect(fingerprint(fp.slice(1))).toBeNull();
    expect(fingerprint(fp.toUpperCase())).toBeNull();
    expect(fingerprint(undefined)).toBeNull();
  });
});

describe('external links', () => {
  it('allows only https links to listed hosts', () => {
    expect(allowedExternal('https://chatgpt.com/')?.href).toBe('https://chatgpt.com/');
    expect(allowedExternal('https://claude.ai/new')).not.toBeNull();
    for (const bad of [
      'http://chatgpt.com/',
      'https://chatgpt.com.evil.example/',
      'https://evil.example/?u=chatgpt.com',
      'https://user:pw@chatgpt.com/',
      'https://chatgpt.com:8443/',
      'javascript:alert(1)',
      'file:///etc/passwd',
      'data:text/html,hi',
      'tauri://localhost',
      '//chatgpt.com',
      'not a url',
    ]) {
      expect(allowedExternal(bad), bad).toBeNull();
    }
  });
  it('holds the catalogue key pages and the one-tap page from the build, exact https hosts only', () => {
    expect(ALLOWED_HOSTS).toEqual(linkHosts.hosts);
    expect(Object.isFrozen(ALLOWED_HOSTS)).toBe(true);
    for (const host of ['chatgpt.com', 'claude.ai', 'gemini.google.com', 'openrouter.ai', 'aistudio.google.com', 'console.groq.com']) {
      expect(ALLOWED_HOSTS, host).toContain(host);
    }
    expect(allowedExternal('https://openrouter.ai/auth?callback_url=http%3A%2F%2Flocalhost%3A3000%2Fcb')).not.toBeNull();
    expect(allowedExternal('https://aistudio.google.com/apikey')).not.toBeNull();
    expect(allowedExternal('https://evil.openrouter.ai/')).toBeNull();
    expect(allowedExternal('https://google.com/')).toBeNull();
    expect([...ALLOWED_HOSTS].sort()).toEqual([...ALLOWED_HOSTS]);
    for (const host of ALLOWED_HOSTS) {
      expect(host).toMatch(/^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/);
    }
  });
  it('opens an allowed link, through the Rust command inside the desktop app', async () => {
    const opened: string[] = [];
    const invoked: unknown[] = [];
    const web = { tauri: false, invoke: async () => undefined, open: (u: string) => void opened.push(u) };
    expect(await openExternal('https://chatgpt.com/', web)).toBe(true);
    expect(await openExternal('https://evil.example/', web)).toBe(false);
    expect(opened).toEqual(['https://chatgpt.com/']);
    const desktop = { tauri: true, invoke: async (cmd: string, args?: Record<string, unknown>) => void invoked.push([cmd, args]), open: (u: string) => void opened.push(u) };
    expect(await openExternal('https://claude.ai/', desktop)).toBe(true);
    expect(invoked).toEqual([['open_external', { url: 'https://claude.ai/' }]]);
    expect(opened).toHaveLength(1);
  });
});
