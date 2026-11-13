// For apps (docs/spec/02-ux.md section 4.9, desktop only): how do I let another
// program use my allowances? The address to paste, the programs with their own
// key each, and "Add a program" from ready-made settings. A new program's key is
// shown nowhere on screen: one tap copies it to the clipboard, and the app drops it.
import { useCallback, useId, useRef, useState, type FormEvent } from 'react';
import { Icon } from '../components/Icon';
import { ErrorBox, Tag, Updating } from '../components/ui';
import { HomeError } from '../home/client';
import type { App, Preset } from '../home/types';
import { useI18n, type Key } from '../i18n';
import { fmtNumber } from '../i18n/format';
import { redact } from '../lib/redact';
import { useAppClient, useLoad } from '../state/app-client';

const PRIORITY_KEY: Record<App['priority'], Key> = { interactive: 'fa_p_interactive', normal: 'fa_p_normal', background: 'fa_p_background' };

async function copy(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

function AddProgram({ presets, onAdded }: { presets: Preset[]; onAdded: () => void }) {
  const { t } = useI18n();
  const client = useAppClient();
  const [preset, setPreset] = useState<Preset | null>(null);
  const [name, setName] = useState('');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Key | null>(null);
  const [made, setMade] = useState<string | null>(null);
  const [copied, setCopied] = useState<boolean | null>(null);
  // The key lives only in this ref until it is copied or this panel closes. It is never rendered.
  const keyRef = useRef<string | null>(null);
  const nameId = useId();
  const submit = async (e: FormEvent) => {
    e.preventDefault();
    if (!preset) return;
    setBusy(true);
    setError(null);
    try {
      const r = await client.addApp(name.trim(), preset.priority, { dailyRequests: preset.daily_requests, allowPaid: false });
      keyRef.current = r.token;
      setMade(r.app.name);
      onAdded();
    } catch (err) {
      setError(err instanceof HomeError && (err.code === 'invalid_request' || err.kind === 'protocol') ? 'fa_bad_name' : 'fa_failed');
    } finally {
      setBusy(false);
    }
  };
  if (made && preset) {
    return (
      <div className="cardx stack" data-testid="program-made">
        <h3>{t('fa_made', redact(made))}</h3>
        <button
          type="button"
          className="btn primary"
          data-testid="copy-key"
          disabled={keyRef.current === null}
          onClick={() => {
            const key = keyRef.current;
            if (!key) return;
            void copy(key).then((ok) => {
              setCopied(ok);
              if (ok) keyRef.current = null;
            });
          }}
        >
          {t('fa_copy_key')}
        </button>
        <p className="small muted" role="status">
          {copied === true ? t('fa_copied_key') : copied === false ? t('fa_copy_failed') : t('fa_key_once')}
        </p>
        <h4 className="small">{t('fa_steps', redact(preset.title))}</h4>
        <ol className="bul small steps">
          {preset.steps.map((s, i) => (
            <li key={i}>
              <code>{redact(s.split(preset.key_placeholder).join(t('fa_your_key')))}</code>
            </li>
          ))}
        </ol>
        {preset.caveat ? <p className="small muted">{redact(preset.caveat)}</p> : null}
        <button
          type="button"
          className="btn sec small"
          onClick={() => {
            keyRef.current = null;
            setMade(null);
            setPreset(null);
            setName('');
            setCopied(null);
          }}
        >
          {t('fa_done')}
        </button>
      </div>
    );
  }
  return (
    <form className="stack" onSubmit={(e) => void submit(e)} data-testid="add-program">
      <fieldset className="seg">
        <legend>{t('fa_pick')}</legend>
        {presets.map((p) => (
          <label key={p.kind} className="radio">
            <input
              type="radio"
              name="preset"
              checked={preset?.kind === p.kind}
              onChange={() => {
                setPreset(p);
                if (!name) setName(p.kind.replace(/[^a-z0-9-]/g, '').slice(0, 40));
              }}
            />
            {redact(p.title)}
          </label>
        ))}
      </fieldset>
      <label htmlFor={nameId}>
        <b>{t('fa_name')}</b>
      </label>
      <input id={nameId} value={name} onChange={(e) => setName(e.target.value.toLowerCase())} maxLength={40} data-testid="program-name" />
      <p className="small muted">{t('fa_name_b')}</p>
      <button type="submit" className="btn primary" disabled={busy || !preset || !name.trim()} data-testid="program-add">
        {t('fa_add')}
      </button>
      {error ? <p className="small errt" role="alert">{t(error)}</p> : null}
    </form>
  );
}

export function ForApps() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const presets = useLoad(useCallback(() => client.presets(), [client]));
  const programs = useLoad(useCallback(() => client.apps(), [client]));
  const apps = programs.data;
  const loadApps = () => void programs.reload();
  const [copied, setCopied] = useState<boolean | null>(null);
  // The home's own managed programs (Ask, the queue, skills) are not the person's to manage here.
  const mine = (apps ?? []).filter((a) => !a.name.startsWith('home-') && !(a.note ?? '').startsWith('managed by'));
  const endpoint = presets.data?.endpoint ?? null;
  return (
    <div className="col" data-testid="screen-apps" data-loaded={presets.data !== null}>
      {presets.problem ? <ErrorBox icon="alert" what={t(presets.problem === 'unreachable' ? 'un_t_never' : 'er_generic_t')} will={t('un_will')} /> : null}
      <section className="card stack" aria-labelledby="fa-addr-h">
        <h2 id="fa-addr-h">{t('fa_address')}</h2>
        <p className="small muted">{t('fa_address_b')}</p>
        {endpoint ? (
          <div className="row wrap">
            <code className="addr" data-testid="endpoint">
              {redact(endpoint)}
            </code>
            <button type="button" className="btn sec small" onClick={() => void copy(endpoint).then(setCopied)} data-testid="copy-endpoint">
              {t('fa_copy')}
            </button>
          </div>
        ) : (
          <Updating text={t('updating')} />
        )}
        <p className="small okline" role="status">
          {copied === true ? t('fa_copied') : copied === false ? t('fa_copy_failed') : null}
        </p>
      </section>
      <section className="card stack" aria-labelledby="fa-list-h">
        <h2 id="fa-list-h">{t('fa_programs', fmtNumber(mine.length, lang))}</h2>
        {apps === null ? (
          <Updating text={t('updating')} />
        ) : mine.length === 0 ? (
          <p className="small muted">{t('apps_empty_b')}</p>
        ) : (
          <ul className="lines" data-testid="programs">
            {mine.map((a) => (
              <li key={a.name} className="ln">
                <span className="sp">
                  <b>{redact(a.name)}</b>
                  <br />
                  <span className="small muted">
                    {t(PRIORITY_KEY[a.priority] ?? 'fa_p_normal')}
                    {` · `}
                    {a.daily_requests ? t('fa_daily', fmtNumber(a.daily_requests, lang)) : t('fa_no_daily')}
                  </span>
                </span>
                {a.allow_paid ? <Tag kind="warn">{t('fa_paid')}</Tag> : <Tag>{t('fa_free_only')}</Tag>}
              </li>
            ))}
          </ul>
        )}
        <p className="small muted">{t('fa_perm_note')}</p>
      </section>
      <section className="card stack" aria-labelledby="fa-add-h">
        <h2 id="fa-add-h">
          <Icon name="apps" /> {t('fa_add_h')}
        </h2>
        {presets.data ? <AddProgram presets={presets.data.presets} onAdded={loadApps} /> : <Updating text={t('updating')} />}
      </section>
    </div>
  );
}
