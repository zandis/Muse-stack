// Small shared pieces, ported from the mock-up's CSS classes.
import type { ReactNode } from 'react';
import { Icon, type IconName } from './Icon';

/** A thin progress bar. Drawn as SVG so no inline style is needed (the content policy forbids them in markup). */
export function Bar({ percent, low = false }: { percent: number; low?: boolean }) {
  const pct = Math.min(100, Math.max(0, percent));
  return (
    <svg className={`bar${low ? ' low' : ''}`} viewBox="0 0 100 8" preserveAspectRatio="none" aria-hidden="true" focusable="false">
      <rect className="bar-fill" x="0" y="0" width={pct} height="8" />
    </svg>
  );
}

export function Tag({ kind, icon, children }: { kind?: 'ok' | 'warn' | 'bad' | undefined; icon?: IconName | undefined; children: ReactNode }) {
  return (
    <span className={kind ? `tag ${kind}` : 'tag'}>
      {icon ? <Icon name={icon} /> : null}
      {children}
    </span>
  );
}

/**
 * The one error pattern (docs/spec/02-ux.md section 8): what happened in one
 * sentence, what Muse will do by itself, and the buttons for what the person can do.
 */
export function ErrorBox({ icon, what, will, kind = 'bad', actions, testId }: { icon: IconName; what: string; will?: string; kind?: 'bad' | 'warn' | 'info'; actions?: ReactNode; testId?: string }) {
  return (
    <div className={`card errc ${kind}`} role="status" data-testid={testId}>
      <span className="eic">
        <Icon name={icon} />
      </span>
      <div className="errbody">
        <b>{what}</b>
        {will ? <span className="small muted">{will}</span> : null}
        {actions ? <div className="acts">{actions}</div> : null}
      </div>
    </div>
  );
}

/** An empty state: what would appear here, and the one action that fills it. */
export function EmptyBox({ icon, title, body, action, note, testId }: { icon: IconName; title: string; body: string; action?: ReactNode; note?: string; testId?: string }) {
  return (
    <div className="card empty" data-testid={testId}>
      <Icon name={icon} />
      <h2>{title}</h2>
      <p>{body}</p>
      {action}
      {note ? <p className="small later">{note}</p> : null}
    </div>
  );
}

export function Updating({ text }: { text: string }) {
  return (
    <div className="upd" role="status" data-testid="updating">
      <span className="spin-s" aria-hidden="true" />
      {text}
    </div>
  );
}
