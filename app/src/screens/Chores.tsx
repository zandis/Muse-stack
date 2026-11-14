// Chores (docs/spec/02-ux.md section 4.3): what does my helper do for me without
// being asked? A list in plain sentences, each with its last result, next run and
// budget; add by one sentence with "what I understood" for one confirmation (or the
// questions, when the sentence left something open); Run now, Pause, Edit, Delete,
// and its history. The sentences the home writes come in the chore's own language.
import { useCallback, useId, useRef, useState, type FormEvent } from 'react';
import { Icon } from '../components/Icon';
import { useIsDesktop } from '../components/Shell';
import { EmptyBox, ErrorBox, Tag, Updating } from '../components/ui';
import { HomeError } from '../home/client';
import type { Chore, ChoreRun, Understood } from '../home/types';
import { useI18n, type Key } from '../i18n';
import { fmtDate, fmtNumber, fmtTime } from '../i18n/format';
import { redact } from '../lib/redact';
import { problemFrom, takeChoreDraft, useAppClient, useLoad } from '../state/app-client';
import { isPaused } from '../state/reducer';
import { useAppState } from '../state/store';

const RUN_STATUS: Record<string, Key> = {
  done: 'chr_done',
  partial: 'chr_partial',
  waiting_card: 'chr_waiting_card',
  failed: 'chr_failed',
  running: 'chr_running',
  queued: 'chr_queued',
  skipped: 'chr_skipped',
  deferred: 'chr_queued',
};

function when(ts: string | null, lang: 'en' | 'zh-TW'): string | null {
  const ms = ts ? Date.parse(ts) : NaN;
  return Number.isFinite(ms) ? `${fmtDate(ms, lang)} ${fmtTime(ms, lang)}` : null;
}

// ---- "what I understood"

function UnderstoodView({ u, onConfirm, onChange, busy, confirmKey }: { u: Understood; onConfirm: () => void; onChange: () => void; busy: boolean; confirmKey: Key }) {
  const { t } = useI18n();
  if (!u.ok) {
    return (
      <div className="cardx" data-testid="chore-questions" role="status">
        <h3>{t('ch2_questions')}</h3>
        <ul className="bul">
          {u.questions.map((q) => (
            <li key={q.code}>{redact(q.text)}</li>
          ))}
        </ul>
        <button type="button" className="btn sec" onClick={onChange}>
          {t('ch2_change')}
        </button>
      </div>
    );
  }
  const rows: [Key, string][] = [
    ['ch2_what', u.what],
    ['ch2_when', u.when],
    ['ch2_where', u.where],
    ['ch2_budget', u.budget],
    ['ch2_permission', u.permission],
  ];
  return (
    <div className="cardx" data-testid="chore-understood">
      <h3>{t('ch2_understood')}</h3>
      <dl className="kv">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{t(k)}</dt>
            <dd>{redact(v)}</dd>
          </div>
        ))}
      </dl>
      {u.label?.spare ? <p className="small muted">{t('ch2_spare')}</p> : null}
      {[...u.assumed, ...u.notes].map((n) => (
        <p key={n} className="small muted">
          {redact(n)}
        </p>
      ))}
      <div className="btnrow">
        <button type="button" className="btn primary" onClick={onConfirm} disabled={busy} data-testid="chore-confirm">
          {t(confirmKey)}
        </button>
        <button type="button" className="btn sec" onClick={onChange} disabled={busy}>
          {t('ch2_change')}
        </button>
      </div>
    </div>
  );
}

/** Type a sentence; see what the home understood; confirm once. Used for adding and for editing. */
function SentenceFlow({ initial, onDone, onCancel, edit }: { initial: string; onDone: (u: Understood) => Promise<void>; onCancel?: () => void; edit: boolean }) {
  const { t } = useI18n();
  const client = useAppClient();
  const [sentence, setSentence] = useState(initial);
  const [u, setU] = useState<Understood | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<Key | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const id = useId();
  const understand = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!sentence.trim()) return;
    setBusy(true);
    setError(null);
    try {
      setU(await client.understand(sentence.trim()));
    } catch (err) {
      setError(err instanceof HomeError && err.code === 'invalid_request' ? 'ch2_invalid' : problemFrom(err) === 'unreachable' ? 'ch2_offline' : 'ch2_failed');
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    if (!u) return;
    setBusy(true);
    setError(null);
    try {
      await onDone(u);
      setU(null);
      setSentence('');
    } catch (err) {
      setError(err instanceof HomeError && err.code === 'invalid_request' ? 'ch2_invalid' : 'ch2_failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="stack">
      {u ? (
        <UnderstoodView
          u={u}
          busy={busy}
          confirmKey={edit ? 'ch2_save' : 'ch2_add'}
          onConfirm={() => void confirm()}
          onChange={() => {
            setU(null);
            setTimeout(() => inputRef.current?.focus(), 0);
          }}
        />
      ) : (
        <form className="stack" onSubmit={(e) => void understand(e)} data-testid="chore-form">
          <label htmlFor={id}>
            <b>{t(edit ? 'ch2_edit_label' : 'ch2_add_label')}</b>
          </label>
          <textarea id={id} ref={inputRef} rows={2} value={sentence} onChange={(e) => setSentence(e.target.value)} placeholder={t('ch2_ph')} data-testid="chore-sentence" maxLength={500} />
          <div className="btnrow">
            <button type="submit" className="btn primary small" disabled={busy || !sentence.trim()} data-testid="chore-understand">
              {t('ch2_next')}
            </button>
            {onCancel ? (
              <button type="button" className="btn sec small" onClick={onCancel}>
                {t('cancel')}
              </button>
            ) : null}
          </div>
        </form>
      )}
      {error ? <ErrorBox icon="alert" what={t(error)} will={t('ch2_nothing_saved')} testId="chore-error" /> : null}
    </div>
  );
}

// ---- one chore

function History({ id }: { id: string }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const runs = useLoad(useCallback(() => client.choreHistory(id), [client, id]));
  if (runs.problem) return <p className="small muted">{t('ch2_history_failed')}</p>;
  if (!runs.data) return <Updating text={t('updating')} />;
  if (runs.data.length === 0) return <p className="small muted">{t('ch2_history_none')}</p>;
  return (
    <ul className="lines" data-testid="chore-history">
      {runs.data.map((r: ChoreRun) => (
        <li key={r.id} className="ln">
          <span className="sp">
            {t(RUN_STATUS[r.status] ?? 'chr_other')}
            {r.summary ? <span className="small muted">{` · ${redact(r.summary)}`}</span> : null}
          </span>
          <span className="small muted">{when(r.finished_at ?? r.started_at, lang) ?? ''}</span>
        </li>
      ))}
    </ul>
  );
}

function ChoreCard({ chore, selected, onSelect, onChanged, paused, showDetail }: { chore: Chore; selected: boolean; onSelect: () => void; onChanged: () => Promise<void>; paused: boolean; showDetail: boolean }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState<Key | null>(null);
  const [editing, setEditing] = useState(false);
  const [historyKey, setHistoryKey] = useState(0);
  const dialog = useRef<HTMLDialogElement>(null);
  const dialogTitle = useId();
  const act = async (fn: () => Promise<unknown>, ok: Key) => {
    setBusy(true);
    setNote(null);
    try {
      await fn();
      setNote(ok);
      await onChanged();
      setHistoryKey((k) => k + 1);
    } catch (err) {
      setNote(err instanceof HomeError && err.code === 'conflict' ? 'ch2_conflict' : 'ch2_failed');
    } finally {
      setBusy(false);
    }
  };
  const next = when(chore.next_run, lang);
  const last = when(chore.last_run_at, lang);
  return (
    <li className={`card chore${selected ? ' sel' : ''}`} data-testid="chore" data-chore-id={chore.id}>
      <button type="button" className="chorehead" aria-expanded={selected} onClick={onSelect}>
        <b>{redact(chore.sentence)}</b>
        <Icon name="right" className="chev" />
      </button>
      <div className="chips">
        {!chore.enabled ? <Tag kind="warn">{t('ch2_paused')}</Tag> : null}
        {chore.spare_only ? <Tag icon="bolt">{chore.typical_time ? t('ch2_spare_at', chore.typical_time) : t('ch2_spare')}</Tag> : null}
      </div>
      <p className="small muted">
        {last ? t('ch2_last', t(RUN_STATUS[chore.last_status ?? ''] ?? 'chr_other'), last) : t('ch2_never')}
        {chore.last_summary ? ` · ${redact(chore.last_summary)}` : ''}
      </p>
      <p className="small muted">{chore.enabled && next ? t('ch2_next_run', next) : null}</p>
      <p className="small">{redact(chore.budget_text)}</p>
      {selected && showDetail ? (
        <div className="stack chorebody">
          {editing ? (
            <SentenceFlow
              edit
              initial={chore.sentence}
              onCancel={() => setEditing(false)}
              onDone={async (u) => {
                await client.editChore(chore.id, u);
                setEditing(false);
                setNote('ch2_saved');
                await onChanged();
              }}
            />
          ) : (
            <div className="acts">
              <button type="button" className="btn primary small" disabled={busy || paused} onClick={() => void act(() => client.runChore(chore.id), 'ch2_started')} data-testid="chore-run">
                <Icon name="play" />
                {t('ch2_run')}
              </button>
              <button type="button" className="btn sec small" disabled={busy} onClick={() => void act(() => client.setChoreEnabled(chore.id, !chore.enabled), chore.enabled ? 'ch2_paused_ok' : 'ch2_resumed_ok')} data-testid="chore-toggle">
                {t(chore.enabled ? 'pause' : 'resume')}
              </button>
              <button type="button" className="btn sec small" disabled={busy} onClick={() => setEditing(true)}>
                {t('ch2_edit')}
              </button>
              <button type="button" className="btn link small" disabled={busy} onClick={() => dialog.current?.showModal()}>
                {t('ch2_delete')}
              </button>
            </div>
          )}
          {paused ? <p className="small muted">{t('ch2_paused_all')}</p> : null}
          <p className="small okline" role="status" data-testid="chore-note">
            {note ? t(note) : null}
          </p>
          <h3 className="small">{t('ch2_history')}</h3>
          <History key={historyKey} id={chore.id} />
          <dialog ref={dialog} className="sheet" aria-labelledby={dialogTitle}>
            <h2 id={dialogTitle}>{t('ch2_delete_q')}</h2>
            <p className="quote">{redact(chore.sentence)}</p>
            <div className="btnrow">
              <button
                type="button"
                className="btn primary"
                onClick={() => {
                  dialog.current?.close();
                  void act(() => client.deleteChore(chore.id), 'ch2_deleted');
                }}
              >
                {t('ch2_delete')}
              </button>
              <button type="button" className="btn sec" onClick={() => dialog.current?.close()}>
                {t('cancel')}
              </button>
            </div>
          </dialog>
        </div>
      ) : null}
    </li>
  );
}

// ---- the screen

export function Chores() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const desktop = useIsDesktop();
  const paused = isPaused(state);
  const list = useLoad(useCallback(() => client.chores(), [client]));
  const [selected, setSelected] = useState<string | null>(null);
  const [draft] = useState(() => takeChoreDraft());
  const [adding, setAdding] = useState(draft !== null);
  const [added, setAdded] = useState(false);
  const chores = list.data?.chores ?? [];
  const reload = list.reload;

  const add = (
    <section className="card stack" aria-labelledby="chore-add-h" data-testid="chore-add">
      <h2 id="chore-add-h">{t('ch2_add_h')}</h2>
      {adding ? (
        <SentenceFlow
          edit={false}
          initial={draft ?? ''}
          onCancel={() => setAdding(false)}
          onDone={async (u) => {
            const made = await client.addChore(u);
            setAdding(false);
            setAdded(true);
            await reload();
            setSelected(made.id);
          }}
        />
      ) : (
        <button type="button" className="btn primary" onClick={() => setAdding(true)} data-testid="chore-add-btn">
          <Icon name="chores" />
          {t('ch2_add_btn')}
        </button>
      )}
      <p className="small okline" role="status">
        {added ? t('ch2_added') : null}
      </p>
    </section>
  );

  return (
    <div className={desktop ? 'col wide' : 'col'} data-testid="screen-chores" data-loaded={list.data !== null}>
      {list.loading && list.data ? <Updating text={t('updating')} /> : null}
      {list.problem ? (
        <ErrorBox
          icon={list.problem === 'unreachable' ? 'cloud' : 'alert'}
          what={t(list.problem === 'unreachable' ? 'un_t_never' : 'er_generic_t')}
          will={t('un_will')}
          testId="chores-problem"
          actions={
            <button type="button" className="btn sec small" onClick={() => void list.reload()}>
              {t('retry')}
            </button>
          }
        />
      ) : null}
      {list.data && !list.data.can_run ? <ErrorBox icon="info" kind="info" what={t('ch2_no_home')} testId="no-home" /> : null}
      <div className={desktop ? 'split' : 'stack'}>
        <div className="stack">
          {list.data && chores.length === 0 ? <EmptyBox icon="chores" title={t('chores_empty_t')} body={t('chores_empty_b')} testId="chores-empty" /> : null}
          {chores.length > 0 ? (
            <ul className="stack" aria-label={t('nav_chores')} data-testid="chore-list">
              {chores.map((c) => (
                <ChoreCard
                  key={c.id}
                  chore={c}
                  paused={paused}
                  selected={selected === c.id && chores.some((x) => x.id === selected)}
                  showDetail
                  onSelect={() => setSelected(selected === c.id ? null : c.id)}
                  onChanged={reload}
                />
              ))}
            </ul>
          ) : null}
          {!list.data && !list.problem ? <div className="card skeleton" aria-hidden="true" /> : null}
        </div>
        <div className="stack">
          {add}
          <p className="small muted">{t('ch2_count', fmtNumber(chores.length, lang))}</p>
        </div>
      </div>
    </div>
  );
}
