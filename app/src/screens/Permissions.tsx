// Permissions (docs/spec/02-ux.md section 4.6): what is my helper allowed to do, and how
// do I stop it? One card per permission, written as a sentence, with where it is in
// force (one mark per place the home's compiler made something), its limits and end
// date, what it has done so far (linked to Records), and Stop: two taps, then each
// place shown turning off one by one from the home's answer, ending in a plain
// confirmation; a place that could not be turned off is shown as such, with Try again.
// Requests waiting for a decision are the grant cards; declined ones are listed.
// Permissions arise from cards (section 5) in this release: see app/README.md.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Card } from '../components/Card';
import { Icon, type IconName } from '../components/Icon';
import { useIsDesktop } from '../components/Shell';
import { ErrorBox, Updating } from '../components/ui';
import type { DeclinedRequest, HomeRecord, Permission, StopResult, TargetState, TargetStatus } from '../home/types';
import { useI18n, type Key, type T } from '../i18n';
import { fmtNumber, fmtTime } from '../i18n/format';
import { redact } from '../lib/redact';
import { Link } from '../router';
import { useAppClient, useLoad } from '../state/app-client';
import { isPaused, waitingCards } from '../state/reducer';
import { useActions, useAppState } from '../state/store';
import { dateWords, dayHeading, holderWords, orderedTargets, permissionLimits, permissionSentence, recordSentence, TARGET_LABEL, type WordsContext } from './trust-words';

const MARK: Record<TargetState, { icon: IconName; cls: string; words: Key }> = {
  active: { icon: 'check', cls: 'on', words: 'pm2_m_active' },
  pending: { icon: 'hourglass', cls: 'wait', words: 'pm2_m_pending' },
  failed: { icon: 'alert', cls: 'fail', words: 'pm2_m_failed' },
  not_built: { icon: 'info', cls: 'grey', words: 'pm2_m_not_built' },
  revoked: { icon: 'x', cls: 'off', words: 'pm2_m_revoked' },
};

function targetLabel(target: string, t: T): string {
  return t(TARGET_LABEL[target] ?? 'pm2_t_other');
}

/** Places the home lists as not built (GET /v1/mandates `enforcement.not_built`); this is its list today. */
const NOT_BUILT = ['payments', 'connectors', 'health'];

/** How one place ended up after a stop, in plain words. A place that was never built is never said to be "turned off". */
export function offWords(target: string, s: TargetStatus, t: T, notBuilt: readonly string[] = NOT_BUILT): string {
  if (s.state === 'failed') return t('pm2_off_failed');
  if (s.state === 'pending' || s.state === 'active') return t('pm2_off_pending');
  if (s.state === 'not_built' || notBuilt.includes(target)) return t(target === 'payments' ? 'pm2_off_payments' : 'pm2_off_not_built');
  const done: Record<string, Key> = { skills: 'pm2_off_skills', router: 'pm2_off_router', records: 'pm2_off_records' };
  return t(done[target] ?? 'pm2_off_other');
}

function reducedMotion(): boolean {
  return typeof window.matchMedia === 'function' && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

function Marks({ p }: { p: Permission }) {
  const { t } = useI18n();
  return (
    <ul className="pmks" aria-label={t('pm2_marks')} data-testid="perm-marks">
      {orderedTargets(p.targets).map(([target, s]) => {
        const m = MARK[s.state] ?? MARK.pending;
        return (
          <li key={target} className={`pmk ${m.cls}`} data-target={target} data-state={s.state}>
            <Icon name={m.icon} />
            <b>{targetLabel(target, t)}</b>
            {s.state === 'active' ? <span className="sr">{t(m.words)}</span> : <span>{t(m.words)}</span>}
          </li>
        );
      })}
    </ul>
  );
}

// ---- Stop: two taps, then each place turning off

type StopPhase = { kind: 'ask' } | { kind: 'working' } | { kind: 'shown'; result: StopResult; revealed: number } | { kind: 'failed' };

function StopDialog({ p, sentence, notBuilt, onClose }: { p: Permission; sentence: string; notBuilt: readonly string[]; onClose: (stopped: boolean) => void }) {
  const { t } = useI18n();
  const client = useAppClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  const [phase, setPhase] = useState<StopPhase>({ kind: 'ask' });
  const timer = useRef<number | null>(null);
  const stoppedOnce = useRef(false);
  useEffect(() => {
    dialog.current?.showModal();
    return () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    };
  }, []);
  const order = (r: StopResult) => orderedTargets(r.targets);
  const stop = async () => {
    setPhase({ kind: 'working' });
    try {
      const result = await client.stopPermission(p.name);
      const total = order(result).length;
      const step = reducedMotion() ? 0 : 450;
      if (step === 0) {
        setPhase({ kind: 'shown', result, revealed: total });
      } else {
        setPhase({ kind: 'shown', result, revealed: 0 });
        let n = 0;
        const tick = () => {
          n += 1;
          setPhase({ kind: 'shown', result, revealed: n });
          if (n < total) timer.current = window.setTimeout(tick, step);
        };
        timer.current = window.setTimeout(tick, step);
      }
      stoppedOnce.current = true;
    } catch {
      setPhase({ kind: 'failed' });
    }
  };
  const spend = p.actions.includes('pay.spend');
  const program = /^(app|device):/.test(p.holder);
  const finished = phase.kind === 'shown' && phase.revealed >= order(phase.result).length;
  const failedTargets = phase.kind === 'shown' ? order(phase.result).filter(([, s]) => s.state === 'failed' || s.state === 'pending') : [];
  return (
    <dialog ref={dialog} className="sheet" aria-labelledby={title} onClose={() => onClose(stoppedOnce.current)} data-testid="stop-dialog">
      <h2 id={title}>{phase.kind === 'ask' || phase.kind === 'failed' ? t('pm2_stop_t') : finished ? t('pm2_stopped') : t('pm2_stopping')}</h2>
      <p className="quote">{sentence}</p>
      {phase.kind === 'ask' ? (
        <>
          <p>{t(spend ? 'pm2_stop_q_spend' : program ? 'pm2_stop_q_program' : 'pm2_stop_q')}</p>
          <div className="btnrow">
            <button type="button" className="btn danger" onClick={() => void stop()} data-testid="stop-confirm">
              {t('pm2_stop')}
            </button>
            <button type="button" className="btn sec" onClick={() => dialog.current?.close()}>
              {t('cancel')}
            </button>
          </div>
        </>
      ) : null}
      {phase.kind === 'working' ? <Updating text={t('pm2_stopping')} /> : null}
      {phase.kind === 'failed' ? (
        <>
          <ErrorBox icon="alert" what={t('pm2_stop_failed')} will={t('pm2_stop_failed_will')} testId="stop-failed" />
          <div className="btnrow">
            <button type="button" className="btn primary" onClick={() => void stop()}>
              {t('pm2_retry')}
            </button>
            <button type="button" className="btn sec" onClick={() => dialog.current?.close()}>
              {t('cancel')}
            </button>
          </div>
        </>
      ) : null}
      {phase.kind === 'shown' ? (
        <>
          <ol className="steps" aria-live="polite" data-testid="stop-steps">
            <li className="stp done">
              <Icon name="check" />
              <span>{t('pm2_s_withdrawn')}</span>
            </li>
            {order(phase.result).map(([target, s], i) => {
              const shown = i < phase.revealed;
              const bad = s.state === 'failed' || s.state === 'pending';
              const never = s.state === 'not_built' || notBuilt.includes(target);
              return (
                <li key={target} className={`stp ${shown ? (bad ? 'fail' : 'done') : 'wait'}`} data-target={target} data-state={shown ? s.state : 'waiting'}>
                  <Icon name={shown ? (bad ? 'alert' : never ? 'info' : 'check') : 'hourglass'} />
                  <span>
                    <b>{targetLabel(target, t)}</b>
                    {`: ${shown ? offWords(target, s, t, notBuilt) : t('pm2_s_wait')}`}
                  </span>
                </li>
              );
            })}
          </ol>
          {finished ? (
            failedTargets.length ? (
              <ErrorBox
                icon="alert"
                kind="warn"
                what={t('pm2_partial')}
                will={t('pm2_partial_will')}
                testId="stop-partial"
                actions={
                  <button type="button" className="btn sec small" onClick={() => void stop()} data-testid="stop-retry">
                    {t('pm2_retry')}
                  </button>
                }
              />
            ) : (
              <p className="row okline" role="status" data-testid="stop-done">
                <Icon name="check" />
                <b>{t(phase.result.recorded ? 'pm2_done' : 'pm2_done_unrecorded')}</b>
              </p>
            )
          ) : null}
          <div className="btnrow">
            <button type="button" className="btn primary" onClick={() => dialog.current?.close()} disabled={!finished} data-testid="stop-close">
              {t('pm2_close')}
            </button>
          </div>
        </>
      ) : null}
    </dialog>
  );
}

// ---- history (desktop pane)

function History({ p, words }: { p: Permission; words: WordsContext }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const got = useLoad(useCallback(() => client.records({ mandate: p.id, limit: 20 }), [client, p.id]));
  return (
    <div className="stack" data-testid="perm-history">
      <h3>{t('pm2_history')}</h3>
      {got.problem ? <p className="small muted">{t('pm2_history_failed')}</p> : null}
      {!got.data && !got.problem ? <Updating text={t('updating')} /> : null}
      {got.data && got.data.records.length === 0 ? <p className="small muted">{t('pm2_history_none')}</p> : null}
      {got.data && got.data.records.length > 0 ? (
        <ul className="lines">
          {got.data.records.map((r) => (
            <li key={r.seq} className="ln">
              <span className="sp">{recordSentence(r, words)}</span>
              <span className="small muted">{`${dayHeading(r.time, state.now, lang, t)} ${fmtTime(r.time, lang)}`}</span>
            </li>
          ))}
        </ul>
      ) : null}
      <Link to="records" search={`?permission=${encodeURIComponent(p.id)}`} className="btn link small">
        {t('pm2_history_all')}
      </Link>
    </div>
  );
}

// ---- one permission

function useCount(records: HomeRecord[] | null, more: boolean, id: string): { n: number; more: boolean; stoppedAt: number | null } {
  return useMemo(() => {
    const mine = (records ?? []).filter((r) => r.mandate === id);
    const used = mine.filter((r) => r.action !== 'mandate.issue' && r.action !== 'mandate.revoke');
    const stop = mine.find((r) => r.action === 'mandate.revoke');
    return { n: used.length, more, stoppedAt: stop ? stop.time : null };
  }, [records, more, id]);
}

function PermissionCard({
  p,
  words,
  records,
  recordsMore,
  selected,
  onSelect,
  onStopped,
  focusMe,
  notBuilt,
  desktop,
}: {
  p: Permission;
  words: WordsContext;
  records: HomeRecord[] | null;
  recordsMore: boolean;
  selected: boolean;
  onSelect: () => void;
  /** Called when the Stop dialog closes after the permission was stopped. */
  onStopped: () => void;
  /** Move focus to this card's heading once it is shown (after it was stopped and moved to "Stopped or ended"). */
  focusMe: boolean;
  notBuilt: readonly string[];
  desktop: boolean;
}) {
  const { t, lang } = useI18n();
  const state = useAppState();
  const [stopping, setStopping] = useState(false);
  const [lines, setLines] = useState(false);
  const heading = useRef<HTMLHeadingElement>(null);
  const stopBtn = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    if (focusMe) heading.current?.focus();
  }, [focusMe]);
  const sentence = permissionSentence(p, words, p.state !== 'expired' && p.state !== 'stopped', state.now);
  const limits = permissionLimits(p, words);
  const count = useCount(records, recordsMore, p.id);
  const live = p.state === 'active' || p.state === 'not_yet_valid';
  const end = Date.parse(p.not_after);
  const start = Date.parse(p.not_before);
  let when: string;
  if (p.state === 'stopped') when = count.stoppedAt !== null ? t('pm2_stopped_on', dateWords(count.stoppedAt, state.now, lang)) : t('pm2_stopped');
  else if (p.state === 'expired') when = t('pm2_ended_on', dateWords(end, state.now, lang));
  else if (p.state === 'not_yet_valid') when = t('pm2_starts', dateWords(start, state.now, lang));
  else when = t('pm2_until', dateWords(end, state.now, lang));
  const used = count.n === 0 ? t('pm2_unused') : count.more ? t('pm2_used_more', fmtNumber(count.n, lang)) : count.n === 1 ? t('pm2_used_one') : t('pm2_used', fmtNumber(count.n, lang));
  return (
    <li className={`card permc${live ? '' : ' stopped'}${selected ? ' cur' : ''}`} data-testid="perm" data-perm={p.name} data-state={p.state}>
      <h3 ref={heading} tabIndex={-1}>
        {sentence}
      </h3>
      <Marks p={p} />
      <p className="small muted">{[...limits, when].join(' · ')}</p>
      <p className="small">
        {used}
        {count.n > 0 ? (
          <>
            {' · '}
            <Link to="records" search={`?permission=${encodeURIComponent(p.id)}`} className="rlink" testId="perm-records">
              {t('pm2_see_records')}
            </Link>
          </>
        ) : null}
      </p>
      <div className="acts">
        {live ? (
          <button type="button" ref={stopBtn} className="btn danger small" onClick={() => setStopping(true)} aria-label={t('pm2_stop_aria', sentence)} data-testid="perm-stop">
            {t('pm2_stop')}
          </button>
        ) : null}
        {desktop ? (
          <button type="button" className="btn sec small" aria-pressed={selected} onClick={onSelect}>
            {t('pm2_select')}
          </button>
        ) : null}
        <button type="button" className="btn link small" aria-expanded={lines} onClick={() => setLines(!lines)}>
          {t(lines ? 'details_hide' : 'pm2_details')}
        </button>
      </div>
      {lines ? (
        <ul className="bul small" data-testid="perm-lines">
          <li>{redact(p.sentence)}</li>
          {p.lines.map((l) => (
            <li key={l}>{redact(l)}</li>
          ))}
          {orderedTargets(p.targets)
            .filter(([, s]) => s.reason || s.note)
            .map(([target, s]) => (
              <li key={target}>{`${targetLabel(target, t)}: ${redact(s.reason || s.note || '')}`}</li>
            ))}
        </ul>
      ) : null}
      {stopping ? (
        <StopDialog
          p={p}
          sentence={sentence}
          notBuilt={notBuilt}
          onClose={(stopped) => {
            setStopping(false);
            // Focus returns to where the person was: the Stop button; or, once stopped, this card in its new place.
            if (stopped) onStopped();
            else setTimeout(() => stopBtn.current?.focus(), 0);
          }}
        />
      ) : null}
    </li>
  );
}

/** A declined request in this app's own words, from its facts; the home's sentence only when the app has no words for it. */
function declinedSentence(d: DeclinedRequest, words: WordsContext): string {
  const asIf: Permission = {
    id: d.card_id, name: '', holder: d.holder, state: 'active', sentence: d.sentence ?? holderWords(d.holder, words.t), lines: [],
    actions: d.actions, resources: d.resources, limits: [], privacy: '', issued_at: d.declined_at, not_before: d.declined_at, not_after: d.declined_at, targets: {},
  };
  return permissionSentence(asIf, words, false);
}

function Declined({ items, words }: { items: DeclinedRequest[]; words: WordsContext }) {
  const { t, lang } = useI18n();
  const state = useAppState();
  if (items.length === 0) return <p className="small muted">{t('pm2_declined_none')}</p>;
  return (
    <ul className="card rows" data-testid="perm-declined">
      {items.map((d) => (
        <li key={d.card_id} className="mini">
          <span className="ico">
            <Icon name="x" />
          </span>
          <span className="t sp">
            <b>{declinedSentence(d, words)}</b>
            <small>{t('pm2_declined_by', redact(d.device), dateWords(Date.parse(d.declined_at), state.now, lang), dateWords(Date.parse(d.ask_again_after), state.now, lang))}</small>
          </span>
        </li>
      ))}
    </ul>
  );
}

// ---- the screen

function PermissionsBody() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const { answerCard } = useActions();
  const desktop = useIsDesktop();
  const paused = isPaused(state);
  const perms = useLoad(useCallback(() => client.permissions(lang), [client, lang]));
  const declined = useLoad(useCallback(() => client.declined(lang), [client, lang]));
  const records = useLoad(useCallback(() => client.records({ limit: 200 }), [client]));
  const [selected, setSelected] = useState<string | null>(null);
  const [showEnded, setShowEnded] = useState(false);
  const [justStopped, setJustStopped] = useState<string | null>(null);
  const words: WordsContext = useMemo(
    () => ({
      t,
      lang,
      permissions: perms.data ? new Map(perms.data.mandates.map((p) => [p.id, p] as [string, Permission])) : null,
      services: new Map(),
      money: state.status?.money,
    }),
    [t, lang, perms.data, state.status],
  );
  const all = perms.data?.mandates ?? [];
  const live = all.filter((p) => p.state === 'active' || p.state === 'not_yet_valid');
  const ended = all.filter((p) => p.state === 'stopped' || p.state === 'expired');
  const waiting = waitingCards(state).filter((c) => c.kind === 'grant_permission');
  const current = all.find((p) => p.id === selected) ?? live[0] ?? null;
  const changed = () => {
    void perms.reload();
    void records.reload();
  };
  const card = (p: Permission) => (
    <PermissionCard
      key={p.id}
      p={p}
      words={words}
      records={records.data?.records ?? null}
      recordsMore={records.data?.next_before !== null && records.data?.next_before !== undefined}
      selected={desktop && current?.id === p.id}
      onSelect={() => setSelected(p.id)}
      onStopped={() => {
        setShowEnded(true);
        setJustStopped(p.id);
        changed();
      }}
      focusMe={justStopped === p.id && (p.state === 'stopped' || p.state === 'expired')}
      notBuilt={perms.data?.enforcement.not_built ?? NOT_BUILT}
      desktop={desktop}
    />
  );
  const enforcement = perms.data?.enforcement;
  const list = (
    <div className="stack g16">
      <section className="stack" aria-labelledby="pm-waiting">
        <h2 id="pm-waiting" className="sect">
          {t('pm2_waiting')}
        </h2>
        {waiting.length ? (
          <div className="stack" data-testid="perm-waiting">
            {waiting.map((c) => (
              <Card key={c.id} card={c} now={state.now} paused={paused} onAnswer={(decision, always) => void answerCard(c.id, decision, always).then(changed)} />
            ))}
          </div>
        ) : (
          <p className="small muted">{t('pm2_waiting_none')}</p>
        )}
      </section>
      <section className="stack" aria-labelledby="pm-live">
        <h2 id="pm-live" className="sect">
          {t('pm2_in_force')}
        </h2>
        {perms.data && all.length === 0 ? (
          <div className="card empty" data-testid="perms-empty">
            <Icon name="permissions" />
            <h3>{t('perms_empty_t')}</h3>
            <p>{t('perms_empty_b')}</p>
            <Link to="today" className="btn sec">
              {t('pm2_empty_btn')}
            </Link>
          </div>
        ) : null}
        {!perms.data && !perms.problem ? <div className="card skeleton" aria-hidden="true" /> : null}
        {live.length ? (
          <ul className="stack" data-testid="perm-list">
            {live.map(card)}
          </ul>
        ) : null}
      </section>
      {ended.length ? (
        <section className="stack" aria-labelledby="pm-ended">
          <h2 id="pm-ended" className="sect">
            <button type="button" className="btn link" aria-expanded={showEnded} onClick={() => setShowEnded(!showEnded)} data-testid="perm-ended-toggle">
              {`${t('pm2_ended')} (${fmtNumber(ended.length, lang)})`}
            </button>
          </h2>
          {showEnded ? (
            <ul className="stack" data-testid="perm-ended">
              {ended.map(card)}
            </ul>
          ) : null}
        </section>
      ) : null}
      <section className="stack" aria-labelledby="pm-declined">
        <h2 id="pm-declined" className="sect">
          {t('pm2_declined')}
        </h2>
        <p className="small muted">{t('pm2_declined_b')}</p>
        {declined.data ? <Declined items={declined.data} words={words} /> : null}
      </section>
      <section className="card stack" aria-labelledby="pm-how">
        <h2 id="pm-how" className="small">
          {t('pm2_how')}
        </h2>
        <p className="small muted">{t('pm2_how_b')}</p>
        {enforcement ? (
          <p className="small muted" data-testid="perm-enforcement">
            {`${t('pm2_enf_skills', t(enforcement.skills === 'enforce' ? 'pm2_enf_enforce' : enforcement.skills === 'advise' ? 'pm2_enf_advise' : 'pm2_enf_off'))} · ${t('pm2_enf_router')}`}
          </p>
        ) : null}
      </section>
    </div>
  );
  return (
    <div className="col wide" data-testid="screen-permissions" data-loaded={perms.data !== null}>
      {perms.loading && perms.data ? <Updating text={t('updating')} /> : null}
      {perms.problem ? (
        <ErrorBox
          icon={perms.problem === 'unreachable' ? 'cloud' : 'alert'}
          what={t('pm2_problem')}
          will={t('un_will')}
          testId="perms-problem"
          actions={
            <button type="button" className="btn sec small" onClick={() => void perms.reload()}>
              {t('retry')}
            </button>
          }
        />
      ) : null}
      {desktop ? (
        <div className="split">
          {list}
          <aside className="card stack pane" aria-label={t('pm2_history')}>
            {current ? (
              <>
                <h2 className="small">{permissionSentence(current, words, true, state.now)}</h2>
                <History p={current} words={words} />
              </>
            ) : (
              <p className="muted">{t('pm2_pick')}</p>
            )}
          </aside>
        </div>
      ) : (
        list
      )}
    </div>
  );
}

export function Permissions() {
  const { lang } = useI18n();
  return <PermissionsBody key={lang} />;
}
