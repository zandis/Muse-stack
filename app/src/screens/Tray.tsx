// The small panel behind the menu-bar (tray) icon (docs/spec/02-ux.md sections 2.2
// and 4.1): the gauge, spending, and the first waiting card. In the desktop app it
// is its own small window (src-tauri); in a browser it is the page /tray.
import { useEffect, useMemo } from 'react';
import { Card } from '../components/Card';
import { Gauge } from '../components/Gauge';
import { Icon } from '../components/Icon';
import { ConnectionProblem, NoticeArea, PausedBar } from '../components/Shell';
import { Updating } from '../components/ui';
import { isTauri, tauriInvoke } from '../home/tauri';
import { useI18n } from '../i18n';
import { DISPLAY_CURRENCY, fmtMoney, fmtTime, usdToLocal } from '../i18n/format';
import { redact } from '../lib/redact';
import { Link } from '../router';
import { computeGauge, computeSpending } from '../state/gauge';
import { isPaused, waitingCards } from '../state/reducer';
import { useActions, useAppState } from '../state/store';

export function Tray() {
  const { t, lang } = useI18n();
  const state = useAppState();
  const { answerCard, setPaused } = useActions();
  const paused = isPaused(state);
  const status = state.status;
  const gauge = useMemo(() => (status ? computeGauge(status) : null), [status]);
  const spending = useMemo(() => (status ? computeSpending(status) : null), [status]);
  const first = waitingCards(state)[0];
  const title = t('tray_aria');

  useEffect(() => {
    document.title = title;
  }, [title]);

  const pct = gauge?.percent ?? null;
  const about = Boolean(gauge?.estimated) && pct !== null && pct > 0;
  return (
    <main className="tray" aria-label={title} data-testid="tray">
      <h1 className="sr">{title}</h1>
      <PausedBar />
      {!state.loaded && !state.problem ? <Updating text={t('updating')} /> : null}
      <ConnectionProblem compact />
      <NoticeArea />
      {gauge && spending ? (
        <>
          {gauge.connected === 0 ? (
            <p className="muted" data-testid="nothing-connected">
              {t('g_none_t')}
            </p>
          ) : pct === null ? (
            <p className="muted">{t('g_nodaily_t')}</p>
          ) : (
            <div className="g" data-testid="tray-gauge">
              <Gauge percent={pct} size={96} mini estimated={gauge.estimated} paused={paused} />
              <div className="gtx">
                <b data-testid="gauge-percent">{about ? `${t('about')} ${pct}%` : `${pct}%`}</b>
                <span>{paused ? t('paused') : t('g_label')}</span>
                {gauge.nextRefill ? (
                  <span>{pct <= 0 ? t('g_refills', fmtTime(gauge.nextRefill.at, lang)) : t('g_next', fmtTime(gauge.nextRefill.at, lang), redact(gauge.nextRefill.service))}</span>
                ) : null}
              </div>
            </div>
          )}
          <div className="line" data-testid="spending">
            <span>{t('spent_today')}</span>
            <b>{t('x_of_y', fmtMoney(usdToLocal(spending.spentUsd), DISPLAY_CURRENCY, lang), fmtMoney(usdToLocal(spending.capUsd), DISPLAY_CURRENCY, lang))}</b>
          </div>
          {first ? (
            <Card card={first} now={state.now} paused={paused} compact onAnswer={(decision, always) => void answerCard(first.id, decision, always)} />
          ) : (
            <p className="small muted" data-testid="needs-none">
              {t('needs_none')}
            </p>
          )}
        </>
      ) : null}
      <div className="btnrow">
        <button type="button" className={paused ? 'btn primary small' : 'btn sec small'} onClick={() => void setPaused(!paused)} data-testid="tray-pause">
          <Icon name={paused ? 'play' : 'pause'} />
          {t(paused ? 'resume' : 'pause')}
        </button>
        {isTauri() ? (
          <button type="button" className="btn sec small" onClick={() => void tauriInvoke('show_main_window').catch(() => undefined)}>
            {t('tray_open')}
          </button>
        ) : (
          <Link to="today" className="btn sec small">
            {t('tray_open')}
          </Link>
        )}
      </div>
    </main>
  );
}
