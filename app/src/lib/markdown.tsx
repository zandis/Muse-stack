// A small Markdown renderer for answers: text is parsed into a tree of plain
// objects, and the tree is mapped to React elements. No HTML is ever set from a
// string (React escapes all text), so raw HTML in an answer shows as text.
// Supported: headings, paragraphs, lists (one level), quotes, fenced code, tables,
// and inline code, bold, italic and links. Links open only through the allow-listed
// external opener (lib/links.ts); anything else stays as text. Images are never
// loaded: their description is shown instead. No maths, no diagrams.
// Written by hand rather than taken from a package: about 200 lines with no
// dependency is less to audit than a parser and its plug-ins.
import type { ReactNode } from 'react';
import { allowedExternal, openExternal } from './links';
import { redact } from './redact';

export type Inline =
  | { t: 'text'; v: string }
  | { t: 'code'; v: string }
  | { t: 'strong'; c: Inline[] }
  | { t: 'em'; c: Inline[] }
  | { t: 'link'; href: string; c: Inline[] }
  | { t: 'image'; alt: string };

export type Block =
  | { t: 'h'; level: number; c: Inline[] }
  | { t: 'p'; c: Inline[] }
  | { t: 'ul' | 'ol'; items: Inline[][] }
  | { t: 'quote'; c: Inline[] }
  | { t: 'code'; v: string }
  | { t: 'table'; head: Inline[][]; rows: Inline[][][] };

const MAX_INPUT = 200_000;
const MAX_DEPTH = 6;

// ---- inline

const INLINE = /(`+)([\s\S]*?)\1|!\[([^\]]*)\]\(([^)\s]*)[^)]*\)|\[([^\]]+)\]\(([^)\s]*)[^)]*\)|\*\*([\s\S]+?)\*\*|__([\s\S]+?)__|\*([^*\s][^*]*?)\*|_([^_\s][^_]*?)_/g;

export function parseInline(text: string, depth = 0): Inline[] {
  const out: Inline[] = [];
  if (depth > MAX_DEPTH) return [{ t: 'text', v: text }];
  let last = 0;
  for (const m of text.matchAll(INLINE)) {
    const at = m.index ?? 0;
    if (at > last) out.push({ t: 'text', v: text.slice(last, at) });
    if (m[1] !== undefined) out.push({ t: 'code', v: (m[2] ?? '').trim() });
    else if (m[4] !== undefined) out.push({ t: 'image', alt: m[3] ?? '' });
    else if (m[6] !== undefined) out.push({ t: 'link', href: m[6], c: parseInline(m[5] ?? '', depth + 1) });
    else if (m[7] !== undefined || m[8] !== undefined) out.push({ t: 'strong', c: parseInline(m[7] ?? m[8] ?? '', depth + 1) });
    else out.push({ t: 'em', c: parseInline(m[9] ?? m[10] ?? '', depth + 1) });
    last = at + m[0].length;
  }
  if (last < text.length) out.push({ t: 'text', v: text.slice(last) });
  return out;
}

// ---- blocks

const cells = (line: string): string[] =>
  line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split('|')
    .map((c) => c.trim());

const isRule = (line: string) => /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);

export function parseMarkdown(src: string): Block[] {
  const lines = src.slice(0, MAX_INPUT).replace(/\r\n?/g, '\n').split('\n');
  const out: Block[] = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i] ?? '';
    if (!line.trim()) {
      i++;
      continue;
    }
    const fence = /^\s*(```|~~~)/.exec(line);
    if (fence) {
      const body: string[] = [];
      i++;
      while (i < lines.length && !(lines[i] ?? '').trim().startsWith(fence[1] ?? '```')) body.push(lines[i++] ?? '');
      i++;
      out.push({ t: 'code', v: body.join('\n') });
      continue;
    }
    const h = /^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/.exec(line);
    if (h) {
      out.push({ t: 'h', level: (h[1] ?? '#').length, c: parseInline(h[2] ?? '') });
      i++;
      continue;
    }
    if (line.includes('|') && isRule(lines[i + 1] ?? '')) {
      const head = cells(line).map((c) => parseInline(c));
      const rows: Inline[][][] = [];
      i += 2;
      while (i < lines.length && (lines[i] ?? '').includes('|') && (lines[i] ?? '').trim()) rows.push(cells(lines[i++] ?? '').map((c) => parseInline(c)));
      out.push({ t: 'table', head, rows });
      continue;
    }
    const list = /^\s*([-*+]|\d{1,9}[.)])\s+/.exec(line);
    if (list) {
      const ordered = /\d/.test(list[1] ?? '');
      const items: Inline[][] = [];
      const re = ordered ? /^\s*\d{1,9}[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
      while (i < lines.length) {
        const m = re.exec(lines[i] ?? '');
        if (m) items.push(parseInline(m[1] ?? ''));
        else if ((lines[i] ?? '').trim() && /^\s{2,}/.test(lines[i] ?? '') && items.length) items[items.length - 1]?.push({ t: 'text', v: ` ${(lines[i] ?? '').trim()}` });
        else break;
        i++;
      }
      out.push({ t: ordered ? 'ol' : 'ul', items });
      continue;
    }
    if (/^\s*>/.test(line)) {
      const body: string[] = [];
      while (i < lines.length && /^\s*>/.test(lines[i] ?? '')) body.push((lines[i++] ?? '').replace(/^\s*>\s?/, ''));
      out.push({ t: 'quote', c: parseInline(body.join(' ')) });
      continue;
    }
    const para: string[] = [];
    while (i < lines.length && (lines[i] ?? '').trim() && !/^\s*(```|~~~|#{1,6}\s|>|[-*+]\s|\d{1,9}[.)]\s)/.test(lines[i] ?? '')) {
      if ((lines[i] ?? '').includes('|') && isRule(lines[i + 1] ?? '')) break;
      para.push((lines[i++] ?? '').trim());
    }
    if (para.length === 0) para.push((lines[i++] ?? '').trim());
    out.push({ t: 'p', c: parseInline(para.join('\n')) });
  }
  return out;
}

// ---- to React

function renderInline(nodes: Inline[], key = ''): ReactNode[] {
  return nodes.map((n, i) => {
    const k = `${key}${i}`;
    switch (n.t) {
      case 'text':
        return redact(n.v);
      case 'code':
        return <code key={k}>{redact(n.v)}</code>;
      case 'strong':
        return <strong key={k}>{renderInline(n.c, `${k}.`)}</strong>;
      case 'em':
        return <em key={k}>{renderInline(n.c, `${k}.`)}</em>;
      case 'image':
        // Never loaded: an answer must not make the app fetch anything.
        return <span key={k} className="mdimg">{`[${redact(n.alt)}]`}</span>;
      case 'link': {
        const allowed = allowedExternal(n.href);
        if (!allowed) return <span key={k}>{renderInline(n.c, `${k}.`)}</span>;
        return (
          <button key={k} type="button" className="mdlink" onClick={() => void openExternal(allowed.href)}>
            {renderInline(n.c, `${k}.`)}
          </button>
        );
      }
    }
  });
}

export function Markdown({ text }: { text: string }) {
  const blocks = parseMarkdown(text);
  return (
    <div className="md">
      {blocks.map((b, i) => {
        switch (b.t) {
          case 'h': {
            // Answers sit under the screen's own headings, so theirs start at level 3.
            const level = Math.min(6, b.level + 2);
            const H = `h${level}` as 'h3' | 'h4' | 'h5' | 'h6';
            return <H key={i}>{renderInline(b.c)}</H>;
          }
          case 'p':
            return <p key={i}>{renderInline(b.c)}</p>;
          case 'ul':
          case 'ol': {
            const L = b.t;
            return (
              <L key={i}>
                {b.items.map((it, j) => (
                  <li key={j}>{renderInline(it)}</li>
                ))}
              </L>
            );
          }
          case 'quote':
            return <blockquote key={i}>{renderInline(b.c)}</blockquote>;
          case 'code':
            return (
              <pre key={i} tabIndex={0}>
                <code>{redact(b.v)}</code>
              </pre>
            );
          case 'table':
            return (
              <div key={i} className="mdtable" tabIndex={0}>
                <table>
                  <thead>
                    <tr>
                      {b.head.map((c, j) => (
                        <th key={j} scope="col">
                          {renderInline(c)}
                        </th>
                      ))}
                    </tr>
                  </thead>
                  <tbody>
                    {b.rows.map((r, j) => (
                      <tr key={j}>
                        {r.map((c, k) => (
                          <td key={k}>{renderInline(c)}</td>
                        ))}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            );
        }
      })}
    </div>
  );
}
