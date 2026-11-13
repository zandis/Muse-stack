// Rules about the source itself, checked by reading it.
import { readdirSync, readFileSync } from 'node:fs';
import { join, relative, resolve } from 'node:path';
import ts from 'typescript';
import { describe, expect, it } from 'vitest';
import { CONTENT_SECURITY_POLICY, homeProxy, sameOrigin } from '../home-proxy';

const APP = resolve(import.meta.dirname, '..');
const SRC = join(APP, 'src');

function files(dir: string, out: string[] = []): string[] {
  for (const e of readdirSync(dir, { withFileTypes: true })) {
    const p = join(dir, e.name);
    if (e.isDirectory()) files(p, out);
    else if (/\.(ts|tsx|css)$/.test(e.name)) out.push(p);
  }
  return out;
}

const sources = files(SRC);
const read = (p: string) => readFileSync(p, 'utf8');
const rel = (p: string) => relative(APP, p);

/** Text a person could read: at least one letter or CJK character. Punctuation, spaces and symbols alone are fine. */
const READABLE = /[\p{L}]/u;
const TEXT_ATTRIBUTES = new Set(['aria-label', 'aria-description', 'aria-placeholder', 'aria-roledescription', 'aria-valuetext', 'title', 'placeholder', 'alt', 'label', 'summary']);

/** Every bare user-facing string in a .tsx file: JSX text, string children, and text attributes. */
export function bareStrings(fileName: string, code: string): string[] {
  const sf = ts.createSourceFile(fileName, code, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  const found: string[] = [];
  const at = (n: ts.Node) => `${fileName}:${sf.getLineAndCharacterOfPosition(n.getStart()).line + 1}`;
  const literal = (n: ts.Node | undefined): string | null => (n && (ts.isStringLiteral(n) || ts.isNoSubstitutionTemplateLiteral(n)) ? n.text : null);
  const visit = (n: ts.Node) => {
    if (ts.isJsxText(n) && READABLE.test(n.text)) found.push(`${at(n)} text "${n.text.trim()}"`);
    if (ts.isJsxExpression(n) && n.expression && (ts.isJsxElement(n.parent) || ts.isJsxFragment(n.parent))) {
      // {'words'} and {cond ? 'words' : 'other'} as a child
      const check = (e: ts.Expression) => {
        const s = literal(e);
        if (s !== null && READABLE.test(s)) found.push(`${at(e)} text "${s}"`);
        if (ts.isConditionalExpression(e)) {
          check(e.whenTrue);
          check(e.whenFalse);
        }
        if (ts.isBinaryExpression(e)) check(e.right);
        if (ts.isTemplateExpression(e) && (READABLE.test(e.head.text) || e.templateSpans.some((s) => READABLE.test(s.literal.text)))) found.push(`${at(e)} template with words`);
      };
      check(n.expression);
    }
    if (ts.isJsxAttribute(n) && TEXT_ATTRIBUTES.has(n.name.getText())) {
      const init = n.initializer;
      const s = init ? (literal(init) ?? (ts.isJsxExpression(init) ? literal(init.expression) : null)) : null;
      if (s !== null && READABLE.test(s)) found.push(`${at(n)} ${n.name.getText()}="${s}"`);
    }
    ts.forEachChild(n, visit);
  };
  visit(sf);
  return found;
}

describe('strings', () => {
  it('finds bare strings (the check itself works)', () => {
    const bad = `const X = () => <div title="Hello" aria-label={'Hi'}><p>Some words</p>{'more words'}{ok ? 'yes' : t('k')}{\`\${n} left\`}</div>;`;
    expect(bareStrings('x.tsx', bad)).toHaveLength(6);
    const good = `const X = () => <div className="row" data-testid="a" title={t('k')}><p>{t('k')} · {n}%</p>{\`\${n}%\`}</div>;`;
    expect(bareStrings('x.tsx', good)).toEqual([]);
  });

  it('no component holds a user-facing string: all text comes from the dictionaries', () => {
    const found = sources.filter((p) => p.endsWith('.tsx')).flatMap((p) => bareStrings(rel(p), read(p)));
    expect(found).toEqual([]);
  });

  it('only the dictionaries hold Chinese text', () => {
    const cjk = /[㐀-鿿]/;
    const where = sources.filter((p) => cjk.test(read(p).replace(/\/\/.*$/gm, ''))).map(rel);
    expect(where.sort()).toEqual(['src/i18n/en.ts', 'src/i18n/zh-TW.ts']);
  });
});

describe('safety of the source', () => {
  const code = sources.filter((p) => !p.endsWith('.css'));
  const offenders = (re: RegExp, allow: string[] = []) => code.filter((p) => !allow.includes(rel(p)) && re.test(read(p).replace(/\/\/.*$/gm, '').replace(/\/\*[\s\S]*?\*\//g, ''))).map(rel);

  it('never sets HTML from a string', () => {
    expect(offenders(/dangerouslySetInnerHTML|\.innerHTML|\.outerHTML|insertAdjacentHTML|document\.write|createContextualFragment|DOMParser/)).toEqual([]);
  });
  it('never evaluates text as code', () => {
    expect(offenders(/\beval\s*\(|new Function\s*\(|setTimeout\s*\(\s*['"`]|setInterval\s*\(\s*['"`]/)).toEqual([]);
  });
  it('talks to nothing but the home client: one fetch, no sockets, no beacons', () => {
    expect(offenders(/\bfetch\s*\(/, ['src/home/http.ts'])).toEqual([]);
    expect(offenders(/WebSocket|EventSource|XMLHttpRequest|sendBeacon|importScripts|serviceWorker/)).toEqual([]);
  });
  it('opens outside links only in the allow-list opener', () => {
    expect(offenders(/window\.open|location\.(href|assign|replace)\s*[=(]|target=["']_blank/, ['src/lib/links.ts'])).toEqual([]);
  });
  it('has no address of any outside service in code or styles, apart from the allow-list', () => {
    const urls = sources.flatMap((p) => [...read(p).replace(/\/\/ .*$/gm, '').matchAll(/https?:\/\/[^\s'"`)]+/g)].map((m) => `${rel(p)} ${m[0]}`));
    expect(urls).toEqual([]);
    expect(read(join(SRC, 'styles.css'))).not.toMatch(/@import|url\(/);
  });
  it('holds no token: the fake home’s token and anything key-shaped stay out of the app’s source', () => {
    const all = [...sources, join(APP, 'index.html')];
    expect(all.filter((p) => /fake-home-token|Authorization|Bearer\s+[A-Za-z0-9]/.test(read(p).replace(/\/\/.*$/gm, '').replace(/\/[^/\n]*Bearer[^/\n]*\/[gimsuy]*/g, ''))).map(rel)).toEqual([]);
  });
});

describe('the development proxy and the content policy', () => {
  it('forbids inline script, eval and any connection but to the app itself', () => {
    const csp = Object.fromEntries(CONTENT_SECURITY_POLICY.split('; ').map((d) => [d.split(' ')[0], d.split(' ').slice(1).join(' ')]));
    expect(csp['default-src']).toBe("'none'");
    expect(csp['script-src']).toBe("'self'");
    expect(csp['style-src']).toBe("'self'");
    expect(csp['connect-src']).toBe("'self'");
    expect(csp['object-src']).toBe("'none'");
    expect(csp['base-uri']).toBe("'none'");
    expect(CONTENT_SECURITY_POLICY).not.toMatch(/unsafe-inline|unsafe-eval|\*|data:|blob:|https?:/);
  });
  it('adds the token on the server side, from the environment', () => {
    const p = homeProxy({ MUSE_HOME_PORT: '4999', MUSE_HOME_TOKEN: 'abc' })['/v1'];
    expect(p?.target).toBe('http://127.0.0.1:4999');
    expect(p?.headers).toEqual({ Authorization: 'Bearer abc' });
    expect(homeProxy({})['/v1']?.headers).toEqual({});
    expect(() => homeProxy({ MUSE_HOME_PORT: 'evil.example' })).toThrow();
  });
  it('serves only this app’s own pages, so another site cannot borrow the token', () => {
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173', 'sec-fetch-site': 'same-origin', origin: 'http://127.0.0.1:4173' } })).toBe(true);
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173' } })).toBe(true); // curl, tests
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173', 'sec-fetch-site': 'cross-site' } })).toBe(false);
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173', 'sec-fetch-site': 'same-site' } })).toBe(false);
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173', origin: 'http://evil.example' } })).toBe(false);
    expect(sameOrigin({ headers: { host: '127.0.0.1:4173', origin: 'null' } })).toBe(false);
  });
});
