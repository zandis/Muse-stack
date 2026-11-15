// First run (docs/spec/02-ux.md section 3): welcome, one-tap OpenRouter with its two
// plain facts, try it, and the answer with its grey line. The recovery-key step
// is shown only when the home says the vault is wired in (`features.recovery`);
// until then it is left out, and no words are ever made up.
//
// It opens by itself from Today, once: when the home says it was never finished or
// skipped (`settings.first_run === 'pending'`) and no service is connected. Finishing
// or "Skip for now" is written to the home, so it does not open again on any device.
import { useCallback, useEffect, useRef, useState, type FormEvent } from 'react';
import { Icon } from '../components/Icon';
import { ErrorBox } from '../components/ui';
import type { Turn } from '../home/types';
import { useI18n, type Key, type LangChoice } from '../i18n';
import { readStored, writeStored } from '../lib/storage';
import { Markdown } from '../lib/markdown';
import { useRouter, type Route } from '../router';
import { useAppClient, useLoad } from '../state/app-client';
import { greyLine } from '../state/ask';
import { useActions, useAppState } from '../state/store';
import { OneTap, OneTapFacts } from './Services';

export const FIRST_RUN_KEY = 'muse.firstRunDone';
const ONE_TAP_ID = 'openrouter';
const SUGGESTIONS: readonly Key[] = ['fr_s1', 'fr_s2', 'fr_s3'];

type Step = 'welcome' | 'service' | 'try' | 'answer';

/**
 * Opens the first run instead of Today's empty state, once. `connected` is how many services the home
 * has (null while unknown). Only a home that says `first_run: 'pending'` starts it; any doubt (no reply,
 * an older home, this device already finished it) leaves Today as it is.
 */
export function useStartFirstRun(connected: number | null): void {
  const client = useAppClient();
  const { route, go } = useRouter();
  const asked = useRef(false);
  const where = useRef<Route>(route);
  where.current = route;
  useEffect(() => {
    if (connected !== 0 || asked.current || readStored(FIRST_RUN_KEY) === '1') return;
    asked.current = true;
    client
      .settings()
      .then((s) => {
        if (s.first_run === 'pending' && where.current === 'today') go('welcome', true);
      })
      .catch(() => undefined);
  }, [connected, client, go]);
}

export function FirstRun() {
  const { t, lang, choice, setChoice } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const { retry } = useActions();
  const { go } = useRouter();
  const [step, setStep] = useState<Step>('welcome');
  const [text, setText] = useState('');
  const [turn, setTurn] = useState<Turn | null>(null);
  const [busy, setBusy] = useState(false);
  const [useLocal, setUseLocal] = useState(false);
  const [error, setError] = useState<Key | null>(null);
  const heading = useRef<HTMLHeadingElement>(null);
  const services = useLoad(useCallback(() => client.services(), [client]));
  const about = useLoad(useCallback(() => client.about(), [client]));
  const add = services.data?.add.find((a) => a.id === ONE_TAP_ID) ?? null;
  const already = services.data?.connected.some((s) => !s.local) ?? false;
  const local = about.data?.local_runtime === true;

  useEffect(() => {
    heading.current?.focus();
  }, [step]);

  const finish = (how: 'done' | 'skipped') => {
    writeStored(FIRST_RUN_KEY, '1');
    // Remembered on the home, so it does not start again on this or any other device.
    void client.saveSettings({ first_run: how }).catch(() => undefined);
    retry();
    go('today');
  };

  const ask = async (question: string, e?: FormEvent) => {
    e?.preventDefault();
    const q = question.trim();
    if (!q) return;
    setBusy(true);
    setError(null);
    try {
      const r = await client.ask({ text: q, privacy: useLocal ? 'local' : undefined });
      setTurn(r.answer);
      setStep('answer');
    } catch {
      setError('fr_ask_failed');
    } finally {
      setBusy(false);
    }
  };

  const langs: readonly { value: LangChoice; label: Key }[] = [
    { value: 'zh-TW', label: 'st_lang_zh' },
    { value: 'en', label: 'st_lang_en' },
  ];

  return (
    <div className="firstrun" data-testid="first-run" data-step={step}>
      <div className="col">
        <p className="small muted">{t('brand')}</p>
        {step === 'welcome' ? (
          <section className="card stack">
            <h1 ref={heading} tabIndex={-1}>
              {t('fr_welcome')}
            </h1>
            <fieldset className="seg">
              <legend>{t('st_lang')}</legend>
              {langs.map((o) => (
                <label key={o.label} className="radio" lang={o.value === 'zh-TW' ? 'zh-Hant-TW' : 'en'}>
                  <input type="radio" name="fr-language" checked={(choice ?? lang) === o.value} onChange={() => setChoice(o.value)} />
                  {t(o.label)}
                </label>
              ))}
            </fieldset>
            <p>{t('fr_about')}</p>
            <button type="button" className="btn primary block" onClick={() => setStep(already ? 'try' : 'service')} data-testid="fr-continue">
              {t('fr_continue')}
            </button>
          </section>
        ) : null}
        {step === 'service' ? (
          <section className="card stack">
            <h1 ref={heading} tabIndex={-1}>
              {t('fr_service')}
            </h1>
            <OneTap id={ONE_TAP_ID} name={add?.name ?? 'OpenRouter'} onConnected={() => setStep('try')} />
            <OneTapFacts facts={add?.facts ?? { free_per_day: 50, may_train: 'yes' }} />
            {local ? (
              <button type="button" className="btn link small" onClick={() => {
                  setUseLocal(true);
                  setStep('try');
                }}
                data-testid="fr-local">
                {t('fr_local')}
              </button>
            ) : null}
            {state.problem ? <p className="small muted">{t('un_t_never')}</p> : null}
          </section>
        ) : null}
        {step === 'try' ? (
          <section className="card stack">
            <h1 ref={heading} tabIndex={-1}>
              {t('fr_try')}
            </h1>
            <form className="stack" onSubmit={(e) => void ask(text, e)}>
              <label htmlFor="fr-q" className="sr">
                {t('ask_input')}
              </label>
              <textarea id="fr-q" rows={2} value={text} onChange={(e) => setText(e.target.value)} placeholder={t('ask_ph')} data-testid="fr-input" />
              <button type="submit" className="btn primary" disabled={busy || !text.trim()} data-testid="fr-ask">
                {t('ask_send')}
              </button>
            </form>
            <div className="acts">
              {SUGGESTIONS.map((k) => (
                <button key={k} type="button" className="btn sec small" disabled={busy} onClick={() => void ask(t(k))}>
                  {t(k)}
                </button>
              ))}
            </div>
            {busy ? (
              <p className="upd" role="status">
                <span className="spin-s" aria-hidden="true" />
                {t('ask_thinking')}
              </p>
            ) : null}
            {error ? <ErrorBox icon="alert" what={t(error)} will={t('gl_nothing')} /> : null}
          </section>
        ) : null}
        {step === 'answer' && turn ? (
          <section className="card stack">
            <h1 ref={heading} tabIndex={-1}>
              {t('fr_answer')}
            </h1>
            <p className="quote">{turn.question}</p>
            {turn.status === 'answered' ? (
              <>
                <div className="answer-text" data-testid="fr-answer">
                  <Markdown text={turn.text ?? ''} />
                </div>
                <p className="greyline static" data-testid="grey-line">
                  {turn.route ? greyLine(turn.route.line, turn.route.cost, { currency: state.status?.money?.currency ?? 'TWD', lang, t, now: state.now }) : t('gl_nothing')}
                </p>
              </>
            ) : (
              <ErrorBox icon="alert" what={t('fr_ask_failed')} will={t('gl_nothing')} />
            )}
            {about.data?.features.recovery ? <p className="small">{t('fr_recovery')}</p> : null}
            <button type="button" className="btn primary block" onClick={() => finish('done')} data-testid="fr-done">
              <Icon name="check" />
              {t('fr_done')}
            </button>
          </section>
        ) : null}
        {step !== 'answer' ? (
          <button type="button" className="btn link small" onClick={() => finish('skipped')} data-testid="fr-skip">
            {t('fr_skip')}
          </button>
        ) : null}
      </div>
    </div>
  );
}
