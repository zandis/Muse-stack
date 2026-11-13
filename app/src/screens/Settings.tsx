// Settings (docs/spec/02-ux.md section 4.10): the daily spending limit in the
// person's currency (also shown in US dollars), the default privacy badge, the
// language, the currency and its fixed rate, what the helper remembers, the
// recovery key (behind the home's `recovery` flag: the vault is not wired in, so no
// words are ever shown), and About with "Check that this installation is genuine".
// Provider keys are never shown or changed here.
import { useCallback, useId, useRef, useState, type FormEvent } from 'react';
import { ErrorBox, Updating } from '../components/ui';
import { HomeError } from '../home/client';
import type { Badge, Restart, SettingsData, VerifyResult } from '../home/types';
import { useI18n, type Key, type LangChoice } from '../i18n';
import { fmtDate, fmtMoney, fmtNumber } from '../i18n/format';
import { redact } from '../lib/redact';
import { useAppClient, useLoad } from '../state/app-client';
import { useActions } from '../state/store';
import { BADGE_KEY } from './Ask';
import { RestartNote } from './Services';

const LANG_OPTIONS: readonly { value: LangChoice; label: Key }[] = [
  { value: null, label: 'st_lang_system' },
  { value: 'zh-TW', label: 'st_lang_zh' },
  { value: 'en', label: 'st_lang_en' },
];
const BADGES: readonly Badge[] = ['standard', 'private', 'local'];

function LimitForm({ s, onSaved }: { s: SettingsData; onSaved: (s: SettingsData, r: Restart) => void }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const { retry } = useActions();
  const [value, setValue] = useState(s.daily_limit.local);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<Key | null>(null);
  const id = useId();
  const n = Number(value);
  const valid = value.trim() !== '' && Number.isFinite(n) && n >= 0;
  const usd = valid ? n / Number(s.rate) : null;
  const save = async (e: FormEvent) => {
    e.preventDefault();
    if (!valid) return;
    setBusy(true);
    setMsg(null);
    try {
      const r = await client.saveSettings({ daily_limit_local: value.trim() });
      onSaved(r.settings, r.restart);
      setValue(r.settings.daily_limit.local);
      setMsg('st_saved');
      retry();
    } catch (err) {
      setMsg(err instanceof HomeError && err.code === 'invalid_request' ? 'st_limit_bad' : 'st_failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <form className="card stack" onSubmit={(e) => void save(e)} data-testid="limit-form">
      <label htmlFor={id}>
        <b>{t('st_limit')}</b>
      </label>
      <p className="small muted">{t('st_limit_b')}</p>
      <div className="row limitrow">
        <span aria-hidden="true">{fmtMoney(0, s.currency, lang).replace(/[\d.,\s]/g, '')}</span>
        <input id={id} inputMode="decimal" value={value} onChange={(e) => setValue(e.target.value)} data-testid="limit-input" aria-describedby={`${id}-usd`} />
        <button type="submit" className="btn primary small" disabled={busy || !valid || value === s.daily_limit.local} data-testid="limit-save">
          {t('st_save')}
        </button>
      </div>
      <p id={`${id}-usd`} className="small muted" data-testid="limit-usd">
        {usd !== null ? t('st_limit_usd', fmtMoney(usd, 'USD', lang)) : t('st_limit_bad')}
      </p>
      {s.daily_limit.in_step === false ? <p className="small muted">{t('st_limit_router', `US$${s.daily_limit.router_usd ?? ''}`)}</p> : null}
      <p className="small okline" role="status" data-testid="limit-note">
        {msg ? t(msg) : null}
      </p>
    </form>
  );
}

function About() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const about = useLoad(useCallback(() => client.about(), [client]));
  const [verify, setVerify] = useState<VerifyResult | 'busy' | 'failed' | null>(null);
  const a = about.data;
  const checked = a?.catalogue.checked_on ? Date.parse(`${a.catalogue.checked_on}T12:00:00Z`) : NaN;
  return (
    <section className="card stack" aria-labelledby="about-h" data-testid="about">
      <h2 id="about-h">{t('st_about')}</h2>
      {a ? (
        <dl className="kv">
          <div>
            <dt>{t('st_version')}</dt>
            <dd>{redact(a.versions.muse ?? '—')}</dd>
          </div>
          <div>
            <dt>{t('st_catalogue')}</dt>
            <dd>{Number.isFinite(checked) ? t('st_catalogue_v', fmtDate(checked, lang), fmtNumber(a.catalogue.services, lang)) : '—'}</dd>
          </div>
        </dl>
      ) : about.problem ? (
        <p className="small muted">{t('er_generic_t')}</p>
      ) : (
        <Updating text={t('updating')} />
      )}
      <button
        type="button"
        className="btn sec"
        disabled={verify === 'busy'}
        data-testid="verify"
        onClick={() => {
          setVerify('busy');
          client.verify().then(setVerify, () => setVerify('failed'));
        }}
      >
        {t('st_verify')}
      </button>
      {verify === 'busy' ? <Updating text={t('st_verifying')} /> : null}
      {verify === 'failed' ? <p className="small errt">{t('st_verify_failed')}</p> : null}
      {verify && typeof verify === 'object' ? (
        <div className="stack" role="status" data-testid="verify-result">
          <b className={verify.result === 'ok' ? 'okline' : 'errt'}>{t(verify.result === 'ok' ? 'st_verify_ok' : verify.result === 'problem' ? 'st_verify_bad' : 'st_verify_unknown')}</b>
          <ul className="bul small">
            {verify.lines.map((l, i) => (
              <li key={i}>{redact(l.text)}</li>
            ))}
          </ul>
        </div>
      ) : null}
    </section>
  );
}

export function Settings() {
  const { t, lang, choice, setChoice } = useI18n();
  const client = useAppClient();
  const settings = useLoad(useCallback(() => client.settings(), [client]));
  const memory = useLoad(useCallback(() => client.memory(), [client]));
  const about = useLoad(useCallback(() => client.about(), [client]));
  const [restart, setRestart] = useState<Restart | null>(null);
  const [note, setNote] = useState<Key | null>(null);
  const forgetDialog = useRef<HTMLDialogElement>(null);
  const forgetTitle = useId();
  const s = settings.data;
  const recovery = about.data?.features.recovery === true;
  const exportable = about.data?.features.export === true;

  const patch = async (p: Parameters<typeof client.saveSettings>[0]) => {
    setNote(null);
    try {
      const r = await client.saveSettings(p);
      settings.set(r.settings);
      setRestart(r.restart);
      setNote('st_saved');
    } catch {
      setNote('st_failed');
    }
  };

  return (
    <div className="col" data-testid="screen-settings" data-loaded={s !== null}>
      {settings.problem ? (
        <ErrorBox
          icon={settings.problem === 'unreachable' ? 'cloud' : 'alert'}
          what={t(settings.problem === 'unreachable' ? 'un_t_never' : 'er_generic_t')}
          will={t('un_will')}
          testId="settings-problem"
          actions={
            <button type="button" className="btn sec small" onClick={() => void settings.reload()}>
              {t('retry')}
            </button>
          }
        />
      ) : null}
      {s ? (
        <LimitForm
          s={s}
          onSaved={(next, r) => {
            settings.set(next);
            setRestart(r);
          }}
        />
      ) : !settings.problem ? (
        <div className="card skeleton" aria-hidden="true" />
      ) : null}
      <RestartNote restart={restart} onDone={() => void settings.reload()} />
      {s ? (
        <div className="card">
          <fieldset className="seg" data-testid="default-privacy">
            <legend>{t('st_privacy')}</legend>
            {BADGES.map((b) => (
              <label key={b} className="radio">
                <input type="radio" name="privacy" value={b} checked={s.default_privacy === b} onChange={() => void patch({ default_privacy: b })} />
                {t(BADGE_KEY[b])}
              </label>
            ))}
          </fieldset>
        </div>
      ) : null}
      <div className="card">
        <fieldset className="seg" data-testid="language">
          <legend>{t('st_lang')}</legend>
          {LANG_OPTIONS.map((o) => (
            <label key={o.label} className="radio" lang={o.value === 'zh-TW' ? 'zh-Hant-TW' : o.value === 'en' ? 'en' : undefined}>
              <input
                type="radio"
                name="language"
                value={o.value ?? 'system'}
                checked={choice === o.value}
                onChange={() => {
                  setChoice(o.value);
                  // The home writes chores and records in this language too.
                  if (o.value) void client.saveSettings({ language: o.value }).catch(() => undefined);
                }}
              />
              {t(o.label)}
            </label>
          ))}
        </fieldset>
      </div>
      {s ? (
        <div className="card stack">
          <label className="stack">
            <b>{t('st_currency')}</b>
            <select value={s.currency} onChange={(e) => void patch({ currency: e.target.value })} data-testid="currency">
              {s.currencies.map((c) => (
                <option key={c} value={c}>
                  {c}
                </option>
              ))}
            </select>
          </label>
          <p className="small muted">{t('st_rate', fmtMoney(s.rate, s.currency, lang), s.rate_date ?? '')}</p>
        </div>
      ) : null}
      <p className="small okline" role="status" data-testid="settings-note">
        {note ? t(note) : null}
      </p>
      <section className="card stack" aria-labelledby="mem-h" data-testid="memory">
        <h2 id="mem-h">{t('st_memory')}</h2>
        <p className="small">{t('st_memory_none')}</p>
        {memory.data && memory.data.conversations > 0 ? <p className="small muted">{t('st_memory_convs', fmtNumber(memory.data.conversations, lang))}</p> : null}
        <button type="button" className="btn sec" onClick={() => forgetDialog.current?.showModal()} data-testid="forget">
          {t('st_forget')}
        </button>
        <dialog ref={forgetDialog} className="sheet" aria-labelledby={forgetTitle}>
          <h2 id={forgetTitle}>{t('st_forget_q')}</h2>
          <p>{t('st_forget_b')}</p>
          <div className="btnrow">
            <button
              type="button"
              className="btn primary"
              onClick={() => {
                forgetDialog.current?.close();
                client.forget().then(
                  () => {
                    setNote('st_forgotten');
                    void memory.reload();
                  },
                  () => setNote('st_failed'),
                );
              }}
            >
              {t('st_forget')}
            </button>
            <button type="button" className="btn sec" onClick={() => forgetDialog.current?.close()}>
              {t('cancel')}
            </button>
          </div>
        </dialog>
      </section>
      <section className="card stack" aria-labelledby="rec-h" data-testid="recovery">
        <h2 id="rec-h">{t('st_recovery')}</h2>
        <p className="small muted">{t(recovery ? 'st_recovery_on' : 'st_recovery_off')}</p>
      </section>
      <section className="card stack" aria-labelledby="exp-h">
        <h2 id="exp-h">{t('st_export')}</h2>
        <p className="small muted">{t(exportable ? 'st_export_on' : 'st_export_off')}</p>
      </section>
      <About />
    </div>
  );
}
