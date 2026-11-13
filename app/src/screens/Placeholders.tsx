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
import { useI18n, type Key, type LangChoice } from '../i18n';
import { fmtNumber } from '../i18n/format';
import { Link, type Screen } from '../router';
import { computeGauge } from '../state/gauge';
import { useAppState } from '../state/store';

type PlaceholderScreen = Exclude<Screen, 'today' | 'more' | 'settings' | 'services' | 'wallet'>;

const EMPTY: Record<PlaceholderScreen, { icon: IconName; title: Key; body: Key }> = {
  ask: { icon: 'ask', title: 'ask_empty_t', body: 'ask_empty_b' },
  chores: { icon: 'chores', title: 'chores_empty_t', body: 'chores_empty_b' },
  records: { icon: 'records', title: 're_t', body: 're_b' },
  permissions: { icon: 'permissions', title: 'perms_empty_t', body: 'perms_empty_b' },
  devices: { icon: 'devices', title: 'devices_empty_t', body: 'devices_empty_b' },
  apps: { icon: 'apps', title: 'apps_empty_t', body: 'apps_empty_b' },
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
  const action =
    screen === 'records' ? (
      <Link to="ask" className="btn primary">
        {t('re_btn')}
      </Link>
    ) : undefined;
  return (
    <Page screen={screen}>
      <EmptyBox icon={e.icon} title={t(e.title)} body={t(e.body)} note={t('later_build')} {...(action ? { action } : {})} />
    </Page>
  );
}

export function Services() {
  const { t, lang } = useI18n();
  const { status } = useAppState();
  const connected = status ? computeGauge(status).connected : 0;
  return (
    <Page screen="services">
      {connected > 0 ? (
        <EmptyBox icon="services" title={`${t('se_some_t')} · ${t('mo_services', fmtNumber(connected, lang))}`} body={t('se_some_b')} note={t('later_build')} />
      ) : (
        <EmptyBox icon="services" title={t('se_t')} body={t('se_b')} note={t('later_build')} />
      )}
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

const LANG_OPTIONS: readonly { value: LangChoice; label: Key }[] = [
  { value: null, label: 'st_lang_system' },
  { value: 'zh-TW', label: 'st_lang_zh' },
  { value: 'en', label: 'st_lang_en' },
];

export function Settings() {
  const { t, choice, setChoice } = useI18n();
  return (
    <Page screen="settings">
      <div className="card">
        <fieldset className="seg" data-testid="language">
          <legend>{t('st_lang')}</legend>
          {LANG_OPTIONS.map((o) => (
            <label key={o.label} className="radio" lang={o.value === 'zh-TW' ? 'zh-Hant-TW' : o.value === 'en' ? 'en' : undefined}>
              <input type="radio" name="language" value={o.value ?? 'system'} checked={choice === o.value} onChange={() => setChoice(o.value)} />
              {t(o.label)}
            </label>
          ))}
        </fieldset>
      </div>
      <EmptyBox icon="settings" title={t('st_rest_t')} body={t('st_rest_b')} note={t('later_build')} />
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
