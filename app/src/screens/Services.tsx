// Services (docs/spec/02-ux.md section 4.5): what is connected, how much is left,
// and what should I add? One card per connected service; Test it and Disconnect;
// the add list ranked by allowance gained per minute; one-tap services (approve on
// their page, come back) and guided ones (one step at a time beside the service's
// own key page, then the key pasted by the person into a password field, which is
// cleared the moment it is sent and never kept in the app's state); other
// allowances; the person's own subscriptions.
import { useCallback, useEffect, useId, useRef, useState, type FormEvent } from 'react';
import { Icon } from '../components/Icon';
import { useIsDesktop } from '../components/Shell';
import { Bar, EmptyBox, ErrorBox, Tag, Updating } from '../components/ui';
import { HomeError } from '../home/client';
import type { AddService, Bilingual, ConnectedService, Flow, Guide, Restart, ServicesReply, TestOutcome } from '../home/types';
import { useI18n, type Key, type Lang } from '../i18n';
import { fmtDate, fmtNumber, fmtTime } from '../i18n/format';
import { allowCatalogueHosts, openExternal } from '../lib/links';
import { redact } from '../lib/redact';
import { isTauri } from '../home/tauri';
import { useAppClient, useLoad } from '../state/app-client';
import { BADGE_KEY } from './Ask';

const FLOW_POLL_MS = 1500;

export function bi(text: Bilingual | undefined, lang: Lang): string {
  return redact(text?.[lang] ?? text?.en ?? '');
}

const GOOD_FOR: Record<string, Key> = {
  everyday: 'gf_everyday',
  quick_answers: 'gf_quick',
  long_documents: 'gf_long',
  embeddings: 'gf_search',
  reranking: 'gf_search',
};

const STATE_KEY: Partial<Record<ConnectedService['state'], Key>> = { busy: 'sv_busy', used_up: 'sv_used_up', key_refused: 'sv_refused', off: 'sv_off' };

/** The browser that will open the service's page is on the home's own computer: the desktop app, or a page served from this computer. */
export function sameDevice(): boolean {
  return isTauri() || ['127.0.0.1', 'localhost', '[::1]'].includes(window.location.hostname);
}

// ---- after a key is stored: does the router need to start again?

export function RestartNote({ restart, onDone }: { restart: Restart | null; onDone?: () => void }) {
  const { t } = useI18n();
  const client = useAppClient();
  const [state, setState] = useState<'idle' | 'busy' | 'done' | 'failed'>('idle');
  if (!restart?.needed || !restart.proxy_running) return null;
  if (state === 'done') return <p className="small okline">{t('rst_done')}</p>;
  return (
    <ErrorBox
      icon="info"
      kind="info"
      what={t('rst_t')}
      will={restart.managed ? t('rst_managed') : t('rst_manual', restart.command)}
      testId="restart-note"
      actions={
        restart.managed ? (
          <button
            type="button"
            className="btn sec small"
            disabled={state === 'busy'}
            data-testid="restart-now"
            onClick={() => {
              setState('busy');
              client.restartRouter().then(
                () => {
                  setState('done');
                  onDone?.();
                },
                () => setState('failed'),
              );
            }}
          >
            {t(state === 'failed' ? 'rst_again' : 'rst_btn')}
          </button>
        ) : undefined
      }
    />
  );
}

// ---- connected services

function ServiceCard({ s, onRemoved, onReconnect }: { s: ConnectedService; onRemoved: (restart: Restart) => void; onReconnect: () => void }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [test, setTest] = useState<TestOutcome | 'error' | null>(null);
  const dialog = useRef<HTMLDialogElement>(null);
  const dialogTitle = useId();
  const refill = s.refill_at ? Date.parse(s.refill_at) : NaN;
  const retry = s.retry_at ? Date.parse(s.retry_at) : NaN;
  const left = s.left;
  const pct = left && left.limit > 0 ? Math.round((100 * left.remaining) / left.limit) : null;
  const leftText = left ? (s.estimated ? t('sv_left_about', fmtNumber(left.remaining, lang)) : t('sv_left', fmtNumber(left.remaining, lang))) : null;
  const good = [...new Set(s.good_for.map((g) => GOOD_FOR[g]).filter((k): k is Key => k !== undefined))].map((k) => t(k));
  const stateKey = STATE_KEY[s.state];
  const checked = s.checked_on ? Date.parse(`${s.checked_on}T12:00:00Z`) : NaN;
  return (
    <li className="card svc" data-testid="service" data-service={s.id}>
      <div className="row">
        <h3 className="sp">{redact(s.name)}</h3>
        <Tag kind={s.badge === 'standard' ? undefined : 'ok'} icon={s.badge === 'standard' ? undefined : 'lock'}>
          {t(BADGE_KEY[s.badge])}
        </Tag>
      </div>
      {s.local || s.unlimited ? (
        <p className="small">{t('sv_local')}</p>
      ) : left && pct !== null ? (
        <>
          <Bar percent={pct} low={pct < 15} />
          <p className="small">
            <b>{leftText}</b>
            {left.unit === 'tokens' ? ` ${t('sv_unit_words')}` : left.unit === 'requests' ? ` ${t('sv_unit_requests')}` : ''}
          </p>
        </>
      ) : (
        <p className="small muted">{t('sv_left_unknown')}</p>
      )}
      <p className="small muted">
        {Number.isFinite(refill) ? t('g_refills', fmtTime(refill, lang)) : null}
        {Number.isFinite(refill) && good.length ? ' · ' : null}
        {good.length ? t('sv_good_for', good.join(lang === 'zh-TW' ? '、' : ', ')) : null}
      </p>
      {stateKey ? (
        <p className={`small ${s.state === 'key_refused' ? 'errt' : 'warnline'}`} data-testid="service-state">
          {t(stateKey, Number.isFinite(retry) ? fmtTime(retry, lang) : Number.isFinite(refill) ? fmtTime(refill, lang) : '')}
          {s.state === 'key_refused' ? (
            <button type="button" className="btn link small" onClick={onReconnect}>
              {t('sv_reconnect')}
            </button>
          ) : null}
        </p>
      ) : null}
      {Number.isFinite(checked) ? <p className="small muted">{t('sv_checked', fmtDate(checked, lang))}</p> : null}
      <button type="button" className="btn link small" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(open ? 'details_hide' : 'details')}
      </button>
      {open ? (
        <div className="acts">
          {s.can_test ? (
            <button
              type="button"
              className="btn sec small"
              disabled={busy}
              data-testid="service-test"
              onClick={() => {
                setBusy(true);
                setTest(null);
                client.testService(s.id).then(
                  (r) => setTest(r),
                  () => setTest('error'),
                ).finally(() => setBusy(false));
              }}
            >
              {t('sv_test')}
            </button>
          ) : null}
          {s.can_disconnect ? (
            <button type="button" className="btn link small" disabled={busy} onClick={() => dialog.current?.showModal()}>
              {t('sv_disconnect')}
            </button>
          ) : null}
        </div>
      ) : null}
      <p className="small okline" role="status" data-testid="service-test-result">
        {test ? t(test === 'ok' ? 'sv_test_ok' : test === 'refused' ? 'sv_test_refused' : test === 'limited' ? 'sv_test_limited' : 'sv_test_failed') : null}
      </p>
      <dialog ref={dialog} className="sheet" aria-labelledby={dialogTitle}>
        <h2 id={dialogTitle}>{t('sv_disconnect_q', redact(s.name))}</h2>
        <p>{t('sv_disconnect_b')}</p>
        <div className="btnrow">
          <button
            type="button"
            className="btn primary"
            data-testid="service-disconnect"
            onClick={() => {
              dialog.current?.close();
              setBusy(true);
              client.removeService(s.id).then(
                (restart) => onRemoved(restart),
                () => setTest('error'),
              ).finally(() => setBusy(false));
            }}
          >
            {t('sv_disconnect')}
          </button>
          <button type="button" className="btn sec" onClick={() => dialog.current?.close()}>
            {t('cancel')}
          </button>
        </div>
      </dialog>
    </li>
  );
}

// ---- guided key (section 7.1)

function GuideFlow({ id, onClose, onConnected }: { id: string; onClose: () => void; onConnected: (name: string, restart: Restart, test: TestOutcome | null) => void }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const guide = useLoad(useCallback(() => client.guide(id), [client, id]));
  const [step, setStep] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Key | null>(null);
  const [opened, setOpened] = useState<boolean | null>(null);
  const keyRef = useRef<HTMLInputElement>(null);
  const keyId = useId();
  const headRef = useRef<HTMLHeadingElement>(null);
  useEffect(() => {
    headRef.current?.focus();
  }, [step, guide.data]);
  const g: Guide | null = guide.data;
  if (guide.problem) return <ErrorBox icon="alert" what={t('gd_failed')} will={t('ch2_nothing_saved')} actions={<button type="button" className="btn sec small" onClick={onClose}>{t('cancel')}</button>} />;
  if (!g) return <Updating text={t('updating')} />;
  const total = g.steps.length + 1; // the guide's steps, then "paste your key"
  const last = step >= g.steps.length;
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    const input = keyRef.current;
    if (!input) return;
    // Read the key once, clear the field at once: it is never put in React state or kept anywhere in the app.
    const key = input.value.trim();
    input.value = '';
    if (!key) return;
    setBusy(true);
    setError(null);
    try {
      const stored = await client.storeKey(g.provider, key);
      let test: TestOutcome | null = null;
      try {
        test = await client.testService(g.provider);
      } catch {
        test = null;
      }
      onConnected(g.name, stored.restart, test);
    } catch (err) {
      setError(err instanceof HomeError && err.code === 'invalid_request' ? 'gd_bad_key' : 'gd_store_failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <section className="cardx guide" aria-labelledby="guide-h" data-testid="guide">
      <h3 id="guide-h" ref={headRef} tabIndex={-1}>
        {t('gd_title', redact(g.name), fmtNumber(total, lang), g.minutes_estimate ? fmtNumber(g.minutes_estimate, lang) : '?')}
      </h3>
      {g.open_url ? (
        <button
          type="button"
          className="btn sec small"
          data-testid="guide-open"
          onClick={() => {
            void openExternal(g.open_url ?? '').then(setOpened);
          }}
        >
          <Icon name="external" />
          {t('gd_open', redact(new URL(g.open_url).hostname))}
        </button>
      ) : null}
      {opened === false ? <p className="small errt">{t('gd_open_failed')}</p> : null}
      <p className="small muted" data-testid="guide-step">
        {t('gd_step', fmtNumber(step + 1, lang), fmtNumber(total, lang))}
      </p>
      {last ? (
        <form className="stack" onSubmit={(e) => void submit(e)} data-testid="key-form">
          <label htmlFor={keyId}>
            <b>{t('gd_paste')}</b>
          </label>
          <input id={keyId} ref={keyRef} type="password" autoComplete="off" spellCheck={false} data-testid="key-input" maxLength={4096} />
          <p className="small muted">{t('gd_paste_b')}</p>
          <div className="btnrow">
            <button type="submit" className="btn primary" disabled={busy} data-testid="key-submit">
              {t('connect')}
            </button>
            <button type="button" className="btn sec" onClick={() => setStep(step - 1)} disabled={busy || step === 0}>
              {t('gd_back')}
            </button>
          </div>
        </form>
      ) : (
        <div className="stack">
          <b>{bi(g.steps[step]?.title, lang)}</b>
          <p className="small">{bi(g.steps[step]?.detail, lang)}</p>
          <div className="btnrow">
            <button type="button" className="btn primary" onClick={() => setStep(step + 1)} data-testid="guide-next">
              {t('gd_next')}
            </button>
            {step > 0 ? (
              <button type="button" className="btn sec" onClick={() => setStep(step - 1)}>
                {t('gd_back')}
              </button>
            ) : null}
          </div>
          <button type="button" className="btn link small" onClick={() => setStep(g.steps.length)}>
            {t('gd_wrong')}
          </button>
        </div>
      )}
      {error ? <p className="small errt" role="alert">{t(error)}</p> : null}
      <button type="button" className="btn link small" onClick={onClose}>
        {t('cancel')}
      </button>
    </section>
  );
}

// ---- one tap

/** Start the one-tap flow, open the service's page, and wait for the home to say how it went. */
export function OneTap({ id, name, onConnected, onClose, autoStart = false }: { id: string; name: string; onConnected: (flow: Flow) => void; onClose?: () => void; autoStart?: boolean }) {
  const { t } = useI18n();
  const client = useAppClient();
  const [flow, setFlow] = useState<Flow | null>(null);
  const [error, setError] = useState<Key | null>(null);
  const [busy, setBusy] = useState(false);
  const codeRef = useRef<HTMLInputElement>(null);
  const codeId = useId();
  const started = useRef(false);

  const start = useCallback(async () => {
    setError(null);
    setBusy(true);
    try {
      const f = await client.startOneTap(id, sameDevice());
      const url = f.url ?? '';
      allowCatalogueHosts([(() => {
        try {
          return new URL(url).hostname;
        } catch {
          return null;
        }
      })()]);
      setFlow(f);
      if (!(await openExternal(url))) setError('ot_open_failed');
    } catch {
      setError('ot_failed');
    } finally {
      setBusy(false);
    }
  }, [client, id]);

  useEffect(() => {
    if (autoStart && !started.current) {
      started.current = true;
      const timer = setTimeout(() => void start(), 0);
      return () => clearTimeout(timer);
    }
  }, [autoStart, start]);

  useEffect(() => {
    if (!flow || flow.status !== 'pending' || flow.mode !== 'loopback') return;
    const timer = setTimeout(() => {
      client.flow(flow.state_id).then(
        (f) => {
          setFlow(f);
          if (f.status === 'connected') onConnected(f);
        },
        () => setError('ot_failed'),
      );
    }, FLOW_POLL_MS);
    return () => clearTimeout(timer);
  }, [flow, client, onConnected]);

  const giveCode = async (e: FormEvent) => {
    e.preventDefault();
    const input = codeRef.current;
    if (!input || !flow) return;
    const code = input.value.trim();
    input.value = '';
    if (!code) return;
    setBusy(true);
    try {
      const f = await client.giveCode(flow.state_id, code);
      setFlow(f);
      if (f.status === 'connected') onConnected(f);
    } catch {
      setError('ot_bad_code');
    } finally {
      setBusy(false);
    }
  };

  const failed = flow?.status === 'failed';
  return (
    <div className="stack" data-testid="one-tap" data-flow={flow?.status ?? 'none'}>
      {!flow || failed ? (
        <button type="button" className="btn primary block" disabled={busy} onClick={() => void start()} data-testid="one-tap-start">
          <Icon name="bolt" />
          {t(failed ? 'ot_again' : 'ot_btn', redact(name))}
        </button>
      ) : null}
      {flow && flow.status === 'pending' && flow.mode === 'loopback' ? (
        <p className="upd" role="status">
          <span className="spin-s" aria-hidden="true" />
          {t('ot_waiting', redact(name))}
        </p>
      ) : null}
      {flow && flow.status === 'pending' && flow.mode === 'paste_code' ? (
        <form className="stack" onSubmit={(e) => void giveCode(e)} data-testid="code-form">
          <label htmlFor={codeId}>
            <b>{t('ot_code', redact(name))}</b>
          </label>
          <input id={codeId} ref={codeRef} type="password" autoComplete="off" spellCheck={false} data-testid="code-input" maxLength={512} />
          <button type="submit" className="btn primary" disabled={busy}>
            {t('connect')}
          </button>
        </form>
      ) : null}
      {failed ? <p className="small errt" role="alert">{t(flow?.reason === 'timeout' ? 'ot_timeout' : flow?.reason === 'denied' ? 'ot_denied' : 'ot_failed')}</p> : null}
      {error ? <p className="small errt" role="alert">{t(error)}</p> : null}
      {onClose ? (
        <button
          type="button"
          className="btn link small"
          onClick={() => {
            if (flow?.status === 'pending') void client.cancelFlow(flow.state_id).catch(() => undefined);
            onClose();
          }}
        >
          {t('cancel')}
        </button>
      ) : null}
    </div>
  );
}

// ---- the add list

function AddRow({ a, onPick }: { a: AddService; onPick: () => void }) {
  const { t, lang } = useI18n();
  const facts: string[] = [];
  if (a.minutes) facts.push(t('sv_min', fmtNumber(a.minutes, lang)));
  facts.push(a.card === 'no' ? t('sv_no_card') : a.card === 'yes' ? t('sv_card') : t('sv_unconfirmed'));
  if (a.phone === 'yes') facts.push(t('sv_phone'));
  return (
    <li>
      <button type="button" className="ln" onClick={onPick} data-testid="add-service" data-service={a.id}>
        <span className="sp">
          <b>{redact(a.name)}</b>
          <span className="small muted">{` · ${facts.join(' · ')}`}</span>
          {a.method === 'one_tap' ? (
            <>
              {' '}
              <Tag kind="ok" icon="bolt">
                {t('sv_one_tap')}
              </Tag>
            </>
          ) : null}
        </span>
        <Icon name="right" className="chev" />
      </button>
    </li>
  );
}

// ---- the screen

interface Connected {
  kind: 'connected' | 'removed';
  name: string;
  restart: Restart | null;
  test: TestOutcome | null;
}

export function Services() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const desktop = useIsDesktop();
  const services = useLoad(useCallback(() => client.services(), [client]));
  const [adding, setAdding] = useState<AddService | null>(null);
  const [connected, setConnected] = useState<Connected | null>(null);
  const data: ServicesReply | null = services.data;
  useEffect(() => {
    if (data) allowCatalogueHosts(data.add.map((a) => a.key_host));
  }, [data]);
  const reload = services.reload;
  const done = useCallback(
    (c: Connected) => {
      setAdding(null);
      setConnected(c);
      void reload();
    },
    [reload],
  );
  const onFlow = useCallback((f: Flow) => done({ kind: 'connected', name: f.name ?? f.provider, restart: f.restart ?? null, test: null }), [done]);

  return (
    <div className={desktop ? 'col wide' : 'col'} data-testid="screen-services" data-loaded={data !== null}>
      {services.loading && data ? <Updating text={t('updating')} /> : null}
      {services.problem ? (
        <ErrorBox
          icon={services.problem === 'unreachable' ? 'cloud' : 'alert'}
          what={t(services.problem === 'unreachable' ? 'un_t_never' : 'er_generic_t')}
          will={t('un_will')}
          testId="services-problem"
          actions={
            <button type="button" className="btn sec small" onClick={() => void services.reload()}>
              {t('retry')}
            </button>
          }
        />
      ) : null}
      {connected ? (
        <div className="card errc ok" role="status" data-testid="connected-note">
          <span className="eic">
            <Icon name="check" />
          </span>
          <div className="errbody">
            <b>{t(connected.kind === 'removed' ? 'sv_removed' : 'sv_connected', redact(connected.name))}</b>
            {connected.test === 'refused' ? <span className="small errt">{t('sv_test_refused')}</span> : null}
            <RestartNote restart={connected.restart} />
          </div>
        </div>
      ) : null}
      {!data && !services.problem ? <div className="card skeleton" aria-hidden="true" /> : null}
      {data ? (
        <div className={desktop ? 'split' : 'stack'}>
          <section className="stack" aria-labelledby="sv-connected-h">
            <h2 className="nbh" id="sv-connected-h">
              {t('sv_connected_h', fmtNumber(data.connected.length, lang))}
            </h2>
            {data.connected.length === 0 ? (
              <EmptyBox icon="services" title={t('se_t')} body={t('se_b')} testId="services-empty" />
            ) : (
              <ul className={desktop ? 'svcgrid' : 'stack'} data-testid="service-list">
                {data.connected.map((s) => (
                  <ServiceCard
                    key={s.id}
                    s={s}
                    onRemoved={(restart) => done({ kind: 'removed', name: s.name, restart, test: null })}
                    onReconnect={() => setAdding({ id: s.id, name: s.name, rank: 0, method: 'guided', minutes: null, steps: null, card: 'unknown', phone: 'unknown', gain: null, key_host: null, facts: { free_per_day: null, may_train: 'unknown' } })}
                  />
                ))}
              </ul>
            )}
          </section>
          <div className="stack">
            <section className="card stack" aria-labelledby="sv-add-h" data-testid="add-list">
              <h2 id="sv-add-h">{t('sv_add_h')}</h2>
              {adding ? (
                adding.method === 'one_tap' ? (
                  <div className="stack">
                    <b>{redact(adding.name)}</b>
                    <OneTapFacts facts={adding.facts} />
                    <OneTap id={adding.id} name={adding.name} onConnected={onFlow} onClose={() => setAdding(null)} />
                  </div>
                ) : (
                  <GuideFlow id={adding.id} onClose={() => setAdding(null)} onConnected={(name, restart, test) => done({ kind: 'connected', name, restart, test })} />
                )
              ) : data.add.length === 0 ? (
                <p className="small muted">{t('sv_add_none')}</p>
              ) : (
                <>
                  <p className="small muted">{t('sv_add_b')}</p>
                  <ul className="lines">
                    {data.add.map((a) => (
                      <AddRow key={a.id} a={a} onPick={() => setAdding(a)} />
                    ))}
                  </ul>
                </>
              )}
            </section>
            <details className="card" data-testid="other-allowances">
              <summary>
                <b>{t('sv_other_h')}</b>
              </summary>
              <p className="small muted">{t('sv_other_b')}</p>
              <ul className="lines">
                {data.resources.map((r) => (
                  <li key={r.id} className="ln">
                    <span className="sp">
                      <b>{bi(r.name, lang)}</b>
                      <br />
                      <span className="small muted">{bi(r.summary, lang)}</span>
                    </span>
                  </li>
                ))}
              </ul>
            </details>
            <details className="card" data-testid="subscriptions">
              <summary>
                <b>{t('sv_subs_h')}</b>
              </summary>
              <p className="small muted">{t('sv_subs_b')}</p>
              <ul className="lines">
                {data.subscriptions.map((s) => (
                  <li key={s.id} className="ln">
                    <span className="sp">{redact(s.name)}</span>
                  </li>
                ))}
              </ul>
            </details>
          </div>
        </div>
      ) : null}
    </div>
  );
}

/** The two plain facts shown before a one-tap connection (first run, section 3). */
export function OneTapFacts({ facts }: { facts: AddService['facts'] | undefined }) {
  const { t, lang } = useI18n();
  if (!facts) return null;
  return (
    <ul className="bul small" data-testid="one-tap-facts">
      {typeof facts.free_per_day === 'number' ? <li>{t('ot_fact_free', fmtNumber(facts.free_per_day, lang))}</li> : null}
      {facts.may_train === 'yes' || facts.may_train === 'varies' ? <li>{t('ot_fact_train')}</li> : facts.may_train === 'no' ? <li>{t('ot_fact_notrain')}</li> : null}
    </ul>
  );
}
