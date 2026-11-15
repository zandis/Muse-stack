// The screens that are not built yet. Each shows its empty state from
// docs/spec/02-ux.md section 8 (what would appear here, and the one action that
// fills it when that action exists) and says plainly that the screen comes in a
// later build. Two things here are real: the language switch in Settings, and
// the "More" list on a phone.
import type { ReactNode } from 'react';
import type { IconName } from '../components/Icon';
import { Icon } from '../components/Icon';
import { moreItems, NAV_KEY, useIsDesktop } from '../components/Shell';
import { EmptyBox } from '../components/ui';
import { useI18n, type Key } from '../i18n';
import { fmtNumber } from '../i18n/format';
import { Link, type Screen } from '../router';
import { computeGauge } from '../state/gauge';
import { useAppState } from '../state/store';

type PlaceholderScreen = Exclude<Screen, 'today' | 'more' | 'settings' | 'services' | 'wallet' | 'ask' | 'chores' | 'apps' | 'records' | 'permissions'>;

const EMPTY: Record<PlaceholderScreen, { icon: IconName; title: Key; body: Key }> = {
  devices: { icon: 'devices', title: 'devices_empty_t', body: 'devices_empty_b' },
};

function Page({ screen, children }: { screen: Screen; children: ReactNode }) {
  return (
    <div className="col" data-testid={`screen-${screen}`}>
      {children}
    </div>
  );
}

export function Placeholder({ screen }: { screen: PlaceholderScreen }) {
  const { t } = useI18n();
  const e = EMPTY[screen];
  return (
    <Page screen={screen}>
      <EmptyBox icon={e.icon} title={t(e.title)} body={t(e.body)} note={t('later_build')} />
    </Page>
  );
}

/** Reached only by its address for now: the menus hide Wallet until a paid feature is first used (section 4.8). */
export function Wallet() {
  const { t } = useI18n();
  return (
    <Page screen="wallet">
      <div>
        <div className="banner testnet" role="note" data-testid="testnet-header">
          <span className="ic">
            <Icon name="info" />
          </span>
          <span className="tx">
            <b>{t('wl_header')}</b>
          </span>
        </div>
        <EmptyBox icon="wallet" title={t('wl_empty_t')} body={t('wl_empty_b')} note={t('later_build')} />
      </div>
    </Page>
  );
}

export function More() {
  const { t, lang } = useI18n();
  const { status } = useAppState();
  const desktop = useIsDesktop();
  const connected = status ? computeGauge(status).connected : 0;
  const subtitle: Partial<Record<Screen, string>> = {
    services: connected > 0 ? t('mo_services', fmtNumber(connected, lang)) : t('mo_services0'),
    permissions: t('mo_permissions'),
    devices: t('mo_devices'),
    settings: t('mo_settings'),
  };
  return (
    <Page screen="more">
      <ul className="card rows">
        {moreItems(desktop).map((s) => (
          <li key={s}>
            <Link to={s} className="r">
              <span className="ico">
                <Icon name={s} />
              </span>
              <span className="t">
                <b>{t(NAV_KEY[s])}</b>
                <small>{subtitle[s]}</small>
              </span>
              <Icon name="right" className="chev" />
            </Link>
          </li>
        ))}
      </ul>
      <p className="small muted">{t('mo_nowallet')}</p>
    </Page>
  );
}
