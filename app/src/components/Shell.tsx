// The frame around every screen (docs/spec/02-ux.md section 2): five bottom tabs
// with Pause in the top bar on a phone, a sidebar with the same items in the same
// order on a desktop. One code base; the window's width decides.
import { useEffect, useRef, useSyncExternalStore, type ReactNode } from 'react';
import { useI18n, type Key } from '../i18n';
import { fmtLongDate, fmtTime } from '../i18n/format';
import { Link, NAV_MAIN, NAV_MORE, useRouter, type Screen } from '../router';
import { isPaused, waitingCards, type NoticeKind } from '../state/reducer';
import { useActions, useAppState } from '../state/store';
import { Gauge } from './Gauge';
import { Icon, type IconName } from './Icon';
import { ErrorBox } from './ui';

export const DESKTOP_QUERY = '(min-width: 800px)';
const NOTICE_MS = 6000;

export const NAV_KEY: Record<Screen, Key> = {
  today: 'nav_today',
  ask: 'nav_ask',
  chores: 'nav_chores',
  records: 'nav_records',
  more: 'nav_more',
  services: 'nav_services',
  permissions: 'nav_permissions',
  devices: 'nav_devices',
  wallet: 'nav_wallet',
  apps: 'nav_apps',
  settings: 'nav_settings',
};

export function useIsDesktop(): boolean {
  return useSyncExternalStore(
    (notify) => {
      const mq = window.matchMedia(DESKTOP_QUERY);
      mq.addEventListener('change', notify);
      return () => mq.removeEventListener('change', notify);
    },
    () => window.matchMedia(DESKTOP_QUERY).matches,
  );
}

/**
 * What "More" holds on this device. Wallet stays out until it exists (section 4.8);
 * nothing on the home says so yet, so for now it never does. "For apps" is desktop only.
 */
export function moreItems(desktop: boolean, walletExists = false): Screen[] {
  return NAV_MORE.filter((s) => (s === 'wallet' ? walletExists : s === 'apps' ? desktop : true));
}

export function PauseButton() {
  const { t } = useI18n();
  const paused = isPaused(useAppState());
  const { setPaused } = useActions();
  return (
    <button type="button" className="pausebtn" aria-pressed={paused} onClick={() => void setPaused(!paused)} data-testid="pause">
      <Icon name="pause" />
      {t('pause')}
    </button>
  );
}

/** "Everything is paused", with Resume. Shown on every screen while paused. */
export function PausedBar() {
  const { t } = useI18n();
  const paused = isPaused(useAppState());
  const { setPaused } = useActions();
  if (!paused) return null;
  return (
    <div className="banner" role="status" data-testid="paused-bar">
      <span className="ic">
        <Icon name="pause" />
      </span>
      <span className="tx">
        <b>{t('pb_t')}</b>
        <span>{t('pb_b')}</span>
      </span>
      <button type="button" className="btn primary small" onClick={() => void setPaused(false)}>
        <Icon name="play" />
        {t('resume')}
      </button>
    </div>
  );
}

const NOTICE: Record<NoticeKind, { what: Key; will?: Key; kind: 'ok' | 'warn' | 'bad'; icon: IconName }> = {
  approved: { what: 'cd_done_yes', kind: 'ok', icon: 'check' },
  approved_always: { what: 'cd_done_always', kind: 'ok', icon: 'check' },
  declined: { what: 'cd_done_no', kind: 'ok', icon: 'check' },
  card_answered: { what: 'er_card_answered_t', will: 'er_card_gone_w', kind: 'warn', icon: 'info' },
  card_expired: { what: 'er_card_expired_t', will: 'er_card_gone_w', kind: 'warn', icon: 'info' },
  card_failed: { what: 'er_card_t', will: 'er_card_w', kind: 'bad', icon: 'alert' },
  pause_failed: { what: 'er_pause_t', will: 'er_pause_w', kind: 'bad', icon: 'alert' },
};

/** What just happened to something the person did: said once, in a live region. */
export function NoticeArea() {
  const { t } = useI18n();
  const { notice } = useAppState();
  const { dismissNotice } = useActions();
  const good = notice ? NOTICE[notice.kind].kind === 'ok' : false;
  useEffect(() => {
    if (!notice || !good) return;
    const id = setTimeout(dismissNotice, NOTICE_MS);
    return () => clearTimeout(id);
  }, [notice, good, dismissNotice]);
  const n = notice ? NOTICE[notice.kind] : null;
  return (
    <div aria-live="polite" data-testid="notice-area">
      {n && notice ? (
        <div className={`card errc ${n.kind}`} data-testid="notice" data-notice={notice.kind}>
          <span className="eic">
            <Icon name={n.icon} />
          </span>
          <div className="errbody">
            <b>{t(n.what)}</b>
            {n.will ? <span className="small muted">{t(n.will)}</span> : null}
          </div>
          <button type="button" className="icon-btn" aria-label={t('dismiss')} onClick={dismissNotice}>
            <Icon name="x" />
          </button>
        </div>
      ) : null}
    </div>
  );
}

/** The home cannot be reached, or refused: the section 8 pattern, with Retry. */
export function ConnectionProblem({ compact = false }: { compact?: boolean }) {
  const { t, lang } = useI18n();
  const state = useAppState();
  const { retry, useThisDevice } = useActions();
  if (!state.problem) return null;
  const retryBtn = (
    <button type="button" className="btn sec small" onClick={retry}>
      {t('retry')}
    </button>
  );
  if (state.problem === 'unreachable') {
    if (state.useThisDevice) return <ErrorBox icon="info" kind="info" what={t('un_using')} will={`${t('un_using_b')} ${t('later_build')}`} actions={retryBtn} testId="using-this-device" />;
    const what = state.lastOkAt === null ? t('un_t_never') : t('un_t', fmtTime(state.lastOkAt, lang));
    return (
      <ErrorBox
        icon="cloud"
        what={what}
        will={t('un_will')}
        testId="unreachable"
        actions={
          <>
            {retryBtn}
            {compact ? null : (
              <button type="button" className="btn primary small" onClick={useThisDevice}>
                {t('un_use')}
              </button>
            )}
          </>
        }
      />
    );
  }
  const auth = state.problem === 'auth';
  return <ErrorBox icon="alert" what={t(auth ? 'er_auth_t' : 'er_generic_t')} will={t(auth ? 'er_auth_w' : 'er_generic_w')} actions={retryBtn} testId="home-error" />;
}

export function Shell({ screen, children }: { screen: Screen; children: ReactNode }) {
  const { t, lang } = useI18n();
  const { moves, go } = useRouter();
  const state = useAppState();
  const desktop = useIsDesktop();
  const heading = useRef<HTMLHeadingElement>(null);
  const waiting = waitingCards(state).length;
  const more = moreItems(desktop);

  // "More" is a phone screen; on a desktop its items are in the sidebar. "For apps" is desktop only.
  const redirect: Screen | null = desktop && screen === 'more' ? 'services' : !desktop && screen === 'apps' ? 'more' : null;
  useEffect(() => {
    if (redirect) go(redirect, true);
  }, [redirect, go]);

  const title = t(NAV_KEY[screen]);
  useEffect(() => {
    document.title = t('doc_title', title);
  }, [t, title]);

  // Focus management: after a navigation inside the app, focus lands on the new screen's heading.
  useEffect(() => {
    if (moves > 0) heading.current?.focus();
  }, [moves, screen]);

  const dot = (s: Screen) =>
    s === 'today' && waiting > 0 ? (
      <span className="dotb" data-testid="red-dot">
        <span aria-hidden="true">{waiting}</span>
        <span className="sr">{t('dot_sr', waiting)}</span>
      </span>
    ) : null;

  const sub = !desktop && NAV_MORE.includes(screen);
  const current = sub ? 'more' : screen;

  return (
    <div className={desktop ? 'app desktop' : 'app phone'} data-layout={desktop ? 'desktop' : 'phone'}>
      <a className="skip" href="#content">
        {t('skip')}
      </a>
      {desktop ? (
        <nav className="sidebar" aria-label={t('menu_main')}>
          <div className="logo">
            <Gauge percent={72} size={44} mini />
            {t('brand')}
          </div>
          {NAV_MAIN.map((s) => (
            <Link key={s} to={s} className="sbtn" current={screen === s}>
              <Icon name={s} />
              {t(NAV_KEY[s])}
              {dot(s)}
            </Link>
          ))}
          <hr />
          {more.map((s) => (
            <Link key={s} to={s} className="sbtn" current={screen === s}>
              <Icon name={s} />
              {t(NAV_KEY[s])}
            </Link>
          ))}
        </nav>
      ) : null}
      <div className="main">
        <PausedBar />
        <header className="topbar">
          {sub ? (
            <Link to="more" className="icon-btn" label={t('back_more')}>
              <Icon name="left" />
            </Link>
          ) : null}
          <h1 ref={heading} tabIndex={-1} id="page-title">
            {title}
            {screen === 'today' ? <span className="sub">{fmtLongDate(state.now, lang)}</span> : null}
          </h1>
          <PauseButton />
        </header>
        <main className="scroll" id="content" tabIndex={-1}>
          {children}
        </main>
      </div>
      {desktop ? null : (
        <nav className="tabbar" aria-label={t('menu_main')}>
          {[...NAV_MAIN, 'more' as const].map((s) => (
            <Link key={s} to={s} className="tab" current={current === s}>
              <span className="pill">
                <Icon name={s} />
                {dot(s)}
              </span>
              {t(NAV_KEY[s])}
            </Link>
          ))}
        </nav>
      )}
    </div>
  );
}
