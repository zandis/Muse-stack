// The three choices (docs/spec/02-ux.md sections 1.3 and 7.2): Wait, Open in my
// own app, Spend. Only the ones that are real are shown, in that order.
//
// PLACEHOLDER for now. The router's `options=` trailer, which names the choices
// for one job, is not on the home control API yet. Until it is, this component
// is fed by the status snapshot: the next refill time and the person's own
// subscriptions. "Spend" needs an estimate for a job, so it is only drawn when a
// caller passes one (Today never does).
import { useState } from 'react';
import type { Money, Subscription } from '../home/types';
import { useI18n } from '../i18n';
import { fmtCardMoney, fmtTime } from '../i18n/format';
import { allowedExternal, openExternal } from '../lib/links';
import { redact } from '../lib/redact';
import { Icon, type IconName } from './Icon';

export type ChoiceKind = 'wait' | 'open' | 'spend';

export interface ChoiceOption {
  kind: ChoiceKind;
  label: string;
  icon: IconName;
}

export interface ChoicesProps {
  /** When the allowance refills, ms since the epoch; null when the home does not know. */
  refillAt: number | null;
  subscriptions: readonly Subscription[];
  spendEstimate?: Money | null;
  /** Injected in tests. */
  open?: (url: string) => Promise<boolean>;
}

/** The person's first subscription whose address the app is allowed to open. */
export function firstOpenable(subscriptions: readonly Subscription[]): { name: string; url: string } | null {
  for (const s of subscriptions) {
    if (s && typeof s.name === 'string' && typeof s.web_url === 'string' && allowedExternal(s.web_url)) return { name: s.name, url: s.web_url };
  }
  return null;
}

export function Choices({ refillAt, subscriptions, spendEstimate = null, open = openExternal }: ChoicesProps) {
  const { t, lang } = useI18n();
  const [result, setResult] = useState<string | null>(null);
  const time = refillAt === null ? null : fmtTime(refillAt, lang);
  const own = firstOpenable(subscriptions);
  const heading = time ? t('ch_t', time) : t('ch_t_unknown');

  const options: ChoiceOption[] = [{ kind: 'wait', label: time ? t('ch_wait', time) : t('ch_wait_unknown'), icon: 'hourglass' }];
  if (own) options.push({ kind: 'open', label: t('ch_open', redact(own.name)), icon: 'external' });
  if (spendEstimate) options.push({ kind: 'spend', label: t('ch_spend', fmtCardMoney(spendEstimate, lang)), icon: 'wallet' });

  const pick = async (kind: ChoiceKind) => {
    if (kind === 'wait') setResult(t('ch_r_wait'));
    else if (kind === 'spend') setResult(t('ch_r_spend'));
    else if (own) setResult(t((await open(own.url)) ? 'ch_r_open' : 'ch_r_open_fail', redact(own.name)));
  };

  return (
    <div className="cardx choices" role="group" aria-label={heading} data-testid="choices">
      <h3>{heading}</h3>
      {options.map((o) => (
        <button key={o.kind} type="button" className="btn sec block" data-choice={o.kind} onClick={() => void pick(o.kind)}>
          <Icon name={o.icon} />
          {o.label}
        </button>
      ))}
      <p className="small muted">{t(spendEstimate ? 'ch_note' : 'ch_note_free')}</p>
      <p className="small okline" role="status" data-testid="choice-result">
        {result}
      </p>
    </div>
  );
}
