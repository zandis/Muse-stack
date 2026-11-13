// Today (docs/spec/02-ux.md section 4.1): can my helper work right now, and will
// it cost me anything? The gauge and spending, Needs you, Running and Done today,
// and at most one tip. Every state in 4.1 and 8 is handled here.
import { useEffect, useMemo, useState } from 'react';
import { Card } from '../components/Card';
import { Choices } from '../components/Choices';
import { Gauge } from '../components/Gauge';
import { Icon } from '../components/Icon';
import { ConnectionProblem, NoticeArea } from '../components/Shell';
import { Bar, EmptyBox, Updating } from '../components/ui';
import type { Job, Suggestion } from '../home/types';
import { useI18n, type Key, type T } from '../i18n';
import { DISPLAY_CURRENCY, dayKey, fmtMoney, fmtNumber, fmtTime, usdToLocal, type Lang } from '../i18n/format';
import { redact } from '../lib/redact';
import { readStored, writeStored } from '../lib/storage';
import { Link } from '../router';
import { computeGauge, computeSpending, type Gauge as GaugeData, type Spending } from '../state/gauge';
import { isPaused, waitingCards, type State } from '../state/reducer';
import { useActions, useAppState } from '../state/store';
import { chooseTip, parseTipMemory, TIP_STORAGE_KEY, type TipMemory } from '../state/tip';

const SLOW_MS = 600;
const DONE_LINES = 5;

/** True once `on` has stayed true for a moment, so a quick refresh does not flash "Updating…". */
function useSlow(on: boolean, ms = SLOW_MS): boolean {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    if (!on) {
      setSlow(false);
      return;
    }
    const id = setTimeout(() => setSlow(true), ms);
    return () => clearTimeout(id);
  }, [on, ms]);
  return on && slow;
}

// ---- gauge and spending

export function GaugeCard({ gauge, spending, paused, state }: { gauge: GaugeData; spending: Spending; paused: boolean; state: State }) {
  const { t, lang } = useI18n();
  const [details, setDetails] = useState(false);
  if (gauge.connected === 0) {
    return (
      <EmptyBox
        icon="services"
        title={t('g_none_t')}
        body={t('g_none_b')}
        testId="nothing-connected"
        action={
          <Link to="welcome" className="btn primary">
            {t('g_none_btn')}
          </Link>
        }
      />
    );
  }
  const pct = gauge.percent;
  const about = gauge.estimated && pct !== null && pct > 0;
  // The home's own figures in the person's currency when it sends them; else the fixed rate of format.ts.
  const money = state.status?.money;
  const currency = money?.currency ?? DISPLAY_CURRENCY;
  const spent = money ? Number(money.spent_today.local) : usdToLocal(spending.spentUsd);
  const cap = money ? Number(money.daily_limit.local) : usdToLocal(spending.capUsd);
  const spentUsd = money ? Number(money.spent_today.usd) : spending.spentUsd;
  const capUsd = money ? Number(money.daily_limit.usd) : spending.capUsd;
  const ratePerUsd = money ? Number(money.rate) : usdToLocal(1);
  const pctSpent = capUsd > 0 ? Math.min(100, Math.round((100 * spentUsd) / capUsd)) : spending.percent;
  const usedUp = pct !== null && pct <= 0;
  return (
    <div className="card hero" data-testid="gauge-card">
      {pct === null ? (
        <div className="stack" data-testid="no-daily">
          <h2>{t('g_nodaily_t')}</h2>
          <p className="muted">{t('g_nodaily_b')}</p>
        </div>
      ) : (
        <Link to="services" className="gbtn">
          <span className="glabel">{t('g_label')}</span>
          <Gauge percent={pct} size={210} estimated={gauge.estimated} paused={paused}>
            {about ? <span className="gsm">{t('about')}</span> : null}
            <span className="num" data-testid="gauge-percent">
              {`${pct}%`}
            </span>
            <span className="gsm">{paused ? t('paused') : t('g_left')}</span>
          </Gauge>
          {gauge.nextRefill ? (
            <span className="gnext" data-testid="next-refill">
              <Icon name="bolt" />
              {usedUp ? t('g_refills', fmtTime(gauge.nextRefill.at, lang)) : t('g_next', fmtTime(gauge.nextRefill.at, lang), redact(gauge.nextRefill.service))}
            </span>
          ) : null}
          <span className="sr">{t('g_open')}</span>
        </Link>
      )}
      <div className="spend" data-testid="spending">
        <div className="row">
          <span className="sp">{t('spent_today')}</span>
          <b>{t('x_of_y', fmtMoney(spent, currency, lang), fmtMoney(cap, currency, lang))}</b>
        </div>
        <Bar percent={pctSpent} low={pctSpent >= 90} />
        <p className="small muted">{spentUsd > 0 ? t('spent_some', fmtMoney(spent, currency, lang)) : t('spent_none')}</p>
        <button type="button" className="btn link small dlink" aria-expanded={details} aria-controls="spend-details" onClick={() => setDetails(!details)}>
          {t(details ? 'details_hide' : 'details')}
        </button>
        <div id="spend-details" className="techline" hidden={!details}>
          <b>{t('spent_usd')}</b> {t('x_of_y', fmtMoney(spentUsd, 'USD', lang), fmtMoney(capUsd, 'USD', lang))}
          <br />
          {t('spent_rate', fmtMoney(ratePerUsd, currency, lang), fmtMoney(1, 'USD', lang))}
        </div>
      </div>
      {usedUp ? <Choices refillAt={gauge.nextRefill?.at ?? null} subscriptions={state.status?.subscriptions ?? []} /> : null}
    </div>
  );
}

// ---- running and done today

const JOB_LABEL: Record<Job['kind'], Key> = { prompt: 'job_prompt', 'file-summary': 'job_file', batch: 'job_batch' };

function parse(tm: string | null | undefined): number | null {
  const ms = tm ? Date.parse(tm) : NaN;
  return Number.isNaN(ms) ? null : ms;
}

export interface JobLine {
  id: string;
  label: Key;
  right: string;
}

export function jobLines(jobs: readonly Job[], now: number, paused: boolean, t: T, lang: Lang, timeZone?: string): { running: JobLine[]; done: JobLine[] } {
  const running: JobLine[] = [];
  const finished: { at: number; line: JobLine }[] = [];
  const today = dayKey(now, timeZone);
  for (const j of jobs) {
    const label = JOB_LABEL[j.kind] ?? 'job_prompt';
    if (j.status === 'running' || j.status === 'queued' || j.status === 'deferred') {
      const until = j.status === 'deferred' ? parse(j.next_attempt_at) : null;
      const right = paused
        ? t('paused')
        : j.status === 'running'
          ? j.kind === 'batch' && typeof j.progress === 'number'
            ? t('job_progress', fmtNumber(j.progress, lang))
            : t('job_working')
          : until !== null && until > now
            ? t('job_waits_until', fmtTime(until, lang, timeZone))
            : t('job_waiting');
      running.push({ id: j.id, label, right });
    } else if (j.status === 'done' || j.status === 'failed') {
      const at = parse(j.finished_at);
      if (at === null || dayKey(at, timeZone) !== today) continue;
      finished.push({ at, line: { id: j.id, label, right: j.status === 'done' ? fmtTime(at, lang, timeZone) : `${t('job_failed')} · ${fmtTime(at, lang, timeZone)}` } });
    }
  }
  finished.sort((a, b) => b.at - a.at);
  return { running, done: finished.slice(0, DONE_LINES).map((f) => f.line) };
}

function Lines({ lines, t }: { lines: JobLine[]; t: T }) {
  return (
    <ul>
      {lines.map((l) => (
        <li key={l.id}>
          <Link to="records" className="ln">
            <span className="sp">{t(l.label)}</span>
            <span className="muted">{l.right}</span>
            <Icon name="right" className="chev" />
          </Link>
        </li>
      ))}
    </ul>
  );
}

// ---- tip

function useTip(state: State, connected: number): { tip: Suggestion | null; dismiss: () => void } {
  const [memory, setMemory] = useState<TipMemory | null>(() => parseTipMemory(readStored(TIP_STORAGE_KEY)));
  const today = dayKey(state.now);
  const tip = chooseTip(state.status, connected, today, memory);
  const tipId = tip?.id ?? null;
  useEffect(() => {
    // Remember which tip this day's one is, so tomorrow can bring another and today cannot.
    if (tipId && !(memory && memory.day === today)) {
      const next = { day: today, id: tipId, dismissed: false };
      writeStored(TIP_STORAGE_KEY, JSON.stringify(next));
      setMemory(next);
    }
  }, [tipId, today, memory]);
  const dismiss = () => {
    if (!tipId) return;
    const next = { day: today, id: tipId, dismissed: true };
    writeStored(TIP_STORAGE_KEY, JSON.stringify(next));
    setMemory(next);
  };
  return { tip, dismiss };
}

// ---- the screen

export function Today() {
  const { t, lang } = useI18n();
  const state = useAppState();
  const { answerCard } = useActions();
  const paused = isPaused(state);
  const status = state.status;
  const gauge = useMemo(() => (status ? computeGauge(status) : null), [status]);
  const spending = useMemo(() => (status ? computeSpending(status) : null), [status]);
  const waiting = waitingCards(state);
  const { running, done } = useMemo(() => jobLines(state.jobs, state.now, paused, t, lang), [state.jobs, state.now, paused, t, lang]);
  const { tip, dismiss } = useTip(state, gauge?.connected ?? 0);
  const slow = useSlow(state.refreshing);
  const firstLoad = !state.loaded && !state.problem;
  const updating = firstLoad || (slow && state.loaded);

  return (
    <div className="col wide" data-testid="today" data-loaded={state.loaded}>
      {updating ? <Updating text={state.loaded && state.lastOkAt !== null ? t('updating_since', fmtTime(state.lastOkAt, lang)) : t('updating')} /> : null}
      <ConnectionProblem />
      <NoticeArea />
      {status && gauge && spending ? (
        <div className="t3">
          <div className="gcol">
            <GaugeCard gauge={gauge} spending={spending} paused={paused} state={state} />
            {tip ? (
              <div className="tip" data-testid="tip">
                <Icon name="bolt" />
                <span className="sp">{typeof tip.minutes_estimate === 'number' ? t('tip', redact(tip.name), fmtNumber(tip.minutes_estimate, lang)) : t('tip_plain', redact(tip.name))}</span>
                <span className="tipacts">
                  <Link to="services" className="btn sec small">
                    {t('connect')}
                  </Link>
                  <button type="button" className="icon-btn" aria-label={t('tip_x')} onClick={dismiss}>
                    <Icon name="x" />
                  </button>
                </span>
              </div>
            ) : null}
          </div>
          <section className="stack" aria-labelledby="needs-h" data-testid="needs">
            <h2 className="nbh" id="needs-h">
              {waiting.length > 0 ? <span className="ndot" aria-hidden="true" /> : null}
              {t('needs', fmtNumber(waiting.length, lang))}
            </h2>
            {waiting.length > 0 ? (
              waiting.map((c) => <Card key={c.id} card={c} now={state.now} paused={paused} onAnswer={(decision, always) => void answerCard(c.id, decision, always)} />)
            ) : (
              <p className="card muted" data-testid="needs-none">
                {t('needs_none')}
              </p>
            )}
          </section>
          <section className="card lines" data-testid="activity" aria-label={`${t('running')} · ${t('done_today')}`}>
            <h2>{t('running')}</h2>
            {running.length > 0 ? <Lines lines={running} t={t} /> : <p className="muted small">{t('running_none')}</p>}
            <h2>{t('done_today')}</h2>
            {done.length > 0 ? <Lines lines={done} t={t} /> : <p className="muted small">{t('done_none')}</p>}
            <Link to="records" className="btn link small">
              {t('all_records')}
            </Link>
          </section>
        </div>
      ) : firstLoad ? (
        <div className="card skeleton" aria-hidden="true" />
      ) : null}
    </div>
  );
}
