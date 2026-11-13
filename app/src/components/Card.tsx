// The card (docs/spec/02-ux.md section 5): one shape for everything that writes,
// sends or spends. What, the thing, where, the line that belongs to its kind,
// under which permission, what it costs (always present), then Approve and
// Not this time. All seven kinds are drawn here.
//
// Everything in a card comes from the home, so every string goes through
// redact() and is rendered as text.
import { useId, useRef, useState, type ReactNode } from 'react';
import type { Card as CardData, Decision, Money } from '../home/types';
import { useI18n, type Key, type T } from '../i18n';
import { fmtBytes, fmtCardMoney, fmtDateYear, fmtDuration, fmtList, fmtMoney, localToUsd, type Lang } from '../i18n/format';
import { fingerprint, redact } from '../lib/redact';
import { Icon } from './Icon';
import { Tag } from './ui';

const PREVIEW_LINES = 2;
/** Kinds that can become a standing permission from the card ("Always allow this"). */
const CAN_ALWAYS: readonly CardData['kind'][] = ['save_file', 'message'];

const BADGES: Record<string, { key: Key; icon: 'lock' | 'devices' | null }> = {
  private: { key: 'bd_private', icon: 'lock' },
  'on this device': { key: 'bd_device', icon: 'devices' },
  local: { key: 'bd_device', icon: 'devices' },
  standard: { key: 'bd_standard', icon: null },
};

function isMoney(cost: CardData['cost']): cost is Money {
  return 'amount' in cost;
}

/** What is left of the limit after this payment, when both are in the same currency. Negative means over the limit. */
export function afterPayment(cost: Money, limitLeft: Money): number | null {
  if (cost.currency !== limitLeft.currency) return null;
  const a = Number(limitLeft.amount) - Number(cost.amount);
  return Number.isFinite(a) ? Math.round(a * 1e8) / 1e8 : null;
}

function Line({ label, children }: { label: string; children: ReactNode }) {
  return (
    <p className="kvl">
      <b>{label}</b> {children}
    </p>
  );
}

function Bullets({ items }: { items: readonly string[] }) {
  return (
    <ul className="bul">
      {items.map((s, i) => (
        <li key={i}>{redact(s)}</li>
      ))}
    </ul>
  );
}

function Details({ children, t }: { children: ReactNode; t: T }) {
  const [open, setOpen] = useState(false);
  const id = useId();
  return (
    <>
      <button type="button" className="btn link small dlink" aria-expanded={open} aria-controls={id} onClick={() => setOpen(!open)}>
        {t(open ? 'details_hide' : 'details')}
      </button>
      <div id={id} className="techline" hidden={!open}>
        {children}
      </div>
    </>
  );
}

function Preview({ text, t }: { text: string; t: T }) {
  const [all, setAll] = useState(false);
  const lines = redact(text).split('\n');
  const long = lines.length > PREVIEW_LINES || text.length > 160;
  const shown = all || !long ? lines : lines.slice(0, PREVIEW_LINES).map((l) => (l.length > 80 ? `${l.slice(0, 80)}…` : l));
  return (
    <div className="quote">
      {shown.map((l, i) => (
        <p key={i}>{l}</p>
      ))}
      {long ? (
        <button type="button" className="btn link small" aria-expanded={all} onClick={() => setAll(!all)}>
          {t(all ? 'see_less' : 'see_all')}
        </button>
      ) : null}
    </div>
  );
}

/** The line or lines that belong to this kind of card. Returns what blocks approval, if anything. */
function extraFor(card: CardData, t: T, lang: Lang): { lines: ReactNode; over: boolean } {
  switch (card.kind) {
    case 'save_file':
      return {
        over: false,
        lines: typeof card.extra.size_bytes === 'number' ? <Line label={t('cd_size')}>{fmtBytes(card.extra.size_bytes, lang)}</Line> : null,
      };
    case 'message':
      return {
        over: false,
        lines: (
          <>
            {card.extra.to ? <Line label={t('cd_to')}>{redact(card.extra.to)}</Line> : null}
            <Preview text={card.extra.preview} t={t} />
            <p className="small muted">{t(card.extra.draft ? 'cd_draft' : 'cd_sent')}</p>
          </>
        ),
      };
    case 'spend': {
      const after = afterPayment(card.cost, card.extra.limit_left);
      const over = after !== null && after < 0;
      const currency = card.extra.limit_left.currency;
      return {
        over,
        lines: (
          <>
            {card.extra.payee === card.where ? null : <Line label={t('cd_payee')}>{redact(card.extra.payee)}</Line>}
            {after === null ? null : over ? (
              <p className="errt">
                <Icon name="alert" />
                <span>{t('cd_spend_over', fmtMoney(-after, currency, lang))}</span>
              </p>
            ) : (
              <p>{t('cd_spend_after', fmtMoney(after, currency, lang))}</p>
            )}
          </>
        ),
      };
    }
    case 'grant_permission': {
      const ends = card.extra.ends_at ? Date.parse(card.extra.ends_at) : NaN;
      return {
        over: false,
        lines: (
          <>
            <p className="quote">{redact(card.extra.sentence)}</p>
            {card.extra.limits.length > 0 ? (
              <div>
                <b className="small">{t('pm_limits')}</b>
                <Bullets items={card.extra.limits} />
              </div>
            ) : null}
            <Line label={t('pm_ends')}>{Number.isNaN(ends) ? t('pm_noend') : t('pm_until', fmtDateYear(ends, lang))}</Line>
          </>
        ),
      };
    }
    case 'oneoff_credit':
      return {
        over: false,
        lines: (
          <>
            <p className="row note">
              <Icon name="info" />
              <b>{t('cd_oneoff_x')}</b>
            </p>
            {card.extra.credit_left ? <Line label={t('cd_oneoff_left')}>{redact(card.extra.credit_left)}</Line> : null}
          </>
        ),
      };
    case 'send_private': {
      const known = BADGES[card.extra.badge.trim().toLowerCase()];
      return {
        over: false,
        lines: (
          <>
            <Line label={t('cd_private_needs')}>
              {known ? (
                <Tag kind="ok" {...(known.icon ? { icon: known.icon } : {})}>
                  {t(known.key)}
                </Tag>
              ) : (
                <Tag>{redact(card.extra.badge)}</Tag>
              )}
            </Line>
            <Line label={t('cd_private_ok')}>{fmtList(card.extra.services.map(redact), lang)}</Line>
          </>
        ),
      };
    }
    case 'install_skill': {
      const fp = fingerprint(card.extra.sha256);
      return {
        over: false,
        lines: (
          <>
            <div>
              <b className="small">{t('cd_skill_asks')}</b>
              <Bullets items={card.extra.asks_for} />
            </div>
            <Line label={t('cd_skill_by')}>{redact(card.extra.author)}</Line>
            {fp ? (
              <Details t={t}>
                <b>{t('cd_skill_fp')}</b> {fp}
              </Details>
            ) : null}
          </>
        ),
      };
    }
  }
}

export interface CardProps {
  card: CardData;
  /** The app's clock, for the expiry line. */
  now: number;
  paused?: boolean;
  /** The tray panel shows a shorter card: no "Always allow this". */
  compact?: boolean;
  onAnswer: (decision: Decision, always: boolean) => void;
}

export function Card({ card, now, paused = false, compact = false, onAnswer }: CardProps) {
  const { t, lang } = useI18n();
  const titleId = useId();
  const dialog = useRef<HTMLDialogElement>(null);
  const dialogTitle = useId();
  const { lines, over } = extraFor(card, t, lang);
  const left = fmtDuration(Date.parse(card.expires_at) - now, lang);
  const title = redact(card.title);
  const thing = redact(card.thing);
  const cost: CardData['cost'] = card.cost;
  const usd = isMoney(cost) && cost.currency === 'TWD' ? fmtMoney(localToUsd(Number(cost.amount)), 'USD', lang) : null;

  return (
    <article className="cardx actc" aria-labelledby={titleId} data-testid="card" data-kind={card.kind} data-card-id={card.id}>
      <h3 id={titleId}>{title}</h3>
      <p className="thing">{thing}</p>
      {card.where ? <p>{redact(card.where)}</p> : null}
      {lines}
      {card.under.text ? <Line label={t('cd_under')}>{redact(card.under.text)}</Line> : null}
      <Line label={t('cd_cost')}>{isMoney(cost) ? <strong>{fmtCardMoney(cost, lang)}</strong> : t('cd_nothing')}</Line>
      {usd ? (
        <Details t={t}>
          <b>{t('cd_usd')}</b> {t('about')} {usd}
        </Details>
      ) : null}
      <p className="small muted" data-testid="card-expiry">
        {left ? t('cd_expires', left) : t('cd_expires_soon')}
      </p>
      <div className="btnrow">
        <button type="button" className="btn primary" disabled={paused || over} onClick={() => onAnswer('approve', false)}>
          {t('approve')}
        </button>
        <button type="button" className="btn sec" onClick={() => onAnswer('decline', false)}>
          {t('not_now')}
        </button>
      </div>
      {paused ? <p className="small muted">{t('cd_paused')}</p> : null}
      {!compact && !paused && CAN_ALWAYS.includes(card.kind) ? (
        <>
          <button type="button" className="btn link small always" onClick={() => dialog.current?.showModal()}>
            {t('always')}
            <Icon name="right" />
          </button>
          {/* A real dialog: the browser keeps focus inside it, closes it on Escape, and gives focus back afterwards. */}
          <dialog ref={dialog} className="sheet" aria-labelledby={dialogTitle}>
            <h2 id={dialogTitle}>{t('al_title')}</h2>
            <p>{t('al_body')}</p>
            <p className="quote">{t('cd_aria', title, card.where ? `${thing} · ${redact(card.where)}` : thing)}</p>
            <div className="btnrow">
              <button
                type="button"
                className="btn primary"
                onClick={() => {
                  dialog.current?.close();
                  onAnswer('approve', true);
                }}
              >
                {t('al_yes')}
              </button>
              <button type="button" className="btn sec" onClick={() => dialog.current?.close()}>
                {t('cancel')}
              </button>
            </div>
          </dialog>
        </>
      ) : null}
    </article>
  );
}
