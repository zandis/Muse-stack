// Ask (docs/spec/02-ux.md section 4.2): what do I want done now?
// A conversation with a privacy badge and attach (approved folders only) on the
// input; under each answer one grey line (which service, what it cost) that opens
// Details; the sources of an answer from files as file › paragraph with the quoted
// words; "Open in my …"; "Make this a chore"; and when the router cannot answer
// now, the three choices in their fixed order, only the real ones.
import { useCallback, useEffect, useId, useRef, useState, type FormEvent, type ReactNode, type RefObject } from 'react';
import { Icon } from '../components/Icon';
import { useIsDesktop } from '../components/Shell';
import { EmptyBox, ErrorBox } from '../components/ui';
import { HomeError } from '../home/client';
import type { Badge, HandoffApp, Turn } from '../home/types';
import { useI18n, type Key, type T } from '../i18n';
import { fmtDate, fmtList, fmtNumber, fmtTime, type Lang } from '../i18n/format';
import { openExternal } from '../lib/links';
import { Markdown } from '../lib/markdown';
import { redact } from '../lib/redact';
import { useRouter } from '../router';
import { setChoreDraft, useAppClient, useLoad } from '../state/app-client';
import { greyLine, localMoney, openableApp, orderedChoices, reasonKey, type OrderedChoice } from '../state/ask';
import { isPaused } from '../state/reducer';
import { useAppState } from '../state/store';

export const BADGE_KEY: Record<Badge, Key> = { local: 'bd_device', private: 'bd_private', standard: 'bd_standard' };
const BADGES: readonly Badge[] = ['standard', 'private', 'local'];
const PENDING_POLL_MS = 2000;
const QUEUED_POLL_MS = 15000;

interface Outbox {
  text: string;
  privacy: Badge;
  attachments: string[];
  conversationId: string | null;
}

export function useCurrency(): string {
  return useAppState().status?.money?.currency ?? 'TWD';
}

// ---- details behind the grey line

export function AnswerDetails({ turn }: { turn: Turn }) {
  const { t, lang } = useI18n();
  const d = turn.details ?? {};
  const route = turn.route;
  const num = (v: unknown) => (typeof v === 'number' ? fmtNumber(v, lang) : '—');
  const rows: [Key, string][] = [];
  if (route?.name) rows.push(['dt_service', redact(route.name)]);
  if (typeof d.deployment === 'string') rows.push(['dt_model', redact(d.deployment)]);
  if (typeof d.skill === 'string') rows.push(['dt_skill', redact(d.skill)]);
  if (typeof d.requests === 'number') rows.push(['dt_requests', num(d.requests)]);
  rows.push(['dt_tokens', t('dt_tokens_v', num(d.tokens_in), num(d.tokens_out))]);
  rows.push(['dt_cache', t(route?.cache ? 'dt_yes' : 'dt_no')]);
  if (typeof d.task === 'string') rows.push(['dt_task', redact(d.task)]);
  if (typeof d.privacy === 'string' && d.privacy in BADGE_KEY) rows.push(['dt_privacy', t(BADGE_KEY[d.privacy as Badge])]);
  if (route) rows.push(['dt_usd', `US$${route.cost.usd}`]);
  if (d.checked === true) rows.push(['dt_checked', t('dt_yes')]);
  if (d.min_output === true) rows.push(['dt_short', t('dt_yes')]);
  const outside = route && !route.local && !route.cache && route.name;
  return (
    <>
      <dl className="kv" data-testid="answer-details">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{t(k)}</dt>
            <dd>{v}</dd>
          </div>
        ))}
      </dl>
      <p className="small muted" data-testid="left-device">
        {outside ? t(turn.sources?.length ? 'dt_left_files' : 'dt_left', redact(route.name ?? '')) : t('dt_stayed')}
      </p>
    </>
  );
}

// ---- the three choices, from the router's own list

function ChoiceButtons({ turn, onChoose, busy }: { turn: Turn; onChoose: (c: OrderedChoice) => void; busy: boolean }) {
  const { t, lang } = useI18n();
  const currency = useCurrency();
  const choices = orderedChoices(turn.choices);
  const label = (c: OrderedChoice): string =>
    c.kind === 'wait'
      ? c.until !== null
        ? t('ch_wait', fmtTime(c.until, lang))
        : t('ch_wait_unknown')
      : c.kind === 'handoff'
        ? t('ch_open', redact(c.app.name))
        : t('ch_spend_x', localMoney(c.amount, currency, lang, t));
  if (choices.length === 0) return null;
  return (
    <div className="stack" role="group" aria-label={t('ch_group')} data-testid="ask-choices">
      {choices.map((c) => (
        <button key={c.kind} type="button" className="btn sec block" data-choice={c.kind} disabled={busy} onClick={() => onChoose(c)}>
          <Icon name={c.kind === 'wait' ? 'hourglass' : c.kind === 'handoff' ? 'external' : 'wallet'} />
          {label(c)}
        </button>
      ))}
      <p className="small muted">{t(choices.some((c) => c.kind === 'pay') ? 'ch_note' : 'ch_note_free')}</p>
    </div>
  );
}

// ---- one question and its answer

interface TurnProps {
  turn: Turn;
  now: number;
  detailsOpen: boolean;
  inlineDetails: boolean;
  busy: boolean;
  localRuntime: boolean;
  onDetails: () => void;
  onChoose: (c: OrderedChoice) => void;
  onRaise: () => void;
  onDeclineRaise: () => void;
  onUseLocal: () => void;
  onChangeBadge: () => void;
  onContinue: () => void;
}

/** A cut-off answer: one plain line, and "Continue" asks again with more room (once). */
function CutShort({ turn, busy, onContinue }: { turn: Turn; busy: boolean; onContinue: () => void }) {
  const { t } = useI18n();
  const empty = turn.reason === 'empty' || turn.details?.empty_reasoning === true;
  return (
    <div className="row cutshort" data-testid="cut-short">
      <p className="small warnline sp">{t(empty ? 'ask_no_room' : 'ask_cut_short')}</p>
      {turn.can_continue ? (
        <button type="button" className="btn sec small" disabled={busy} onClick={onContinue} data-testid="continue">
          {t('ask_continue')}
        </button>
      ) : null}
    </div>
  );
}

// A PDF source carries its pages: "lease.pdf › page 3, paragraph 2" (one page, and then `paragraphs` are the
// paragraphs on that page) or "lease.pdf › pages 3, 4". Any other file: "notes.md › paragraph 3".
export function sourceLabel(file: string, paragraphs: readonly number[], t: T, lang: Lang, pages: readonly number[] = []): string {
  const list = (ns: readonly number[]) => fmtList(ns.map((n) => fmtNumber(n, lang)), lang);
  if (pages.length > 1) return t('src_pages', redact(file), list(pages));
  if (pages.length === 1) {
    return paragraphs.length === 0 ? t('src_page', redact(file), list(pages)) : t('src_page_line', redact(file), list(pages), list(paragraphs));
  }
  if (paragraphs.length === 0) return redact(file);
  return t('src_line', redact(file), list(paragraphs));
}

function OwnApp({ apps }: { apps: readonly HandoffApp[] | undefined }) {
  const { t } = useI18n();
  const [failed, setFailed] = useState(false);
  const own = openableApp(apps);
  if (!own) return null;
  return (
    <>
      <button
        type="button"
        className="btn sec small"
        data-testid="open-own"
        onClick={() => {
          void openExternal(own.url).then((ok) => setFailed(!ok));
        }}
      >
        <Icon name="external" />
        {t('ch_open', redact(own.app.name))}
      </button>
      {failed ? <span className="small muted">{t('ch_r_open_fail', redact(own.app.name))}</span> : null}
    </>
  );
}

function TurnView(p: TurnProps) {
  const { t, lang } = useI18n();
  const { go } = useRouter();
  const currency = useCurrency();
  const { turn } = p;
  const detailsId = useId();
  let body: ReactNode;
  switch (turn.status) {
    case 'answered': {
      const line = turn.route ? greyLine(turn.route.line, turn.route.cost, { currency, lang, t, now: p.now }) : t('gl_nothing');
      body = (
        <>
          <div className="answer-text" data-testid="answer-text">
            <Markdown text={turn.text ?? ''} />
          </div>
          {turn.cut_short ? <CutShort turn={turn} busy={p.busy} onContinue={p.onContinue} /> : null}
          {turn.sources && turn.sources.length > 0 ? (
            <section aria-label={t('src_h')} className="sources" data-testid="sources">
              <h3 className="small">{t('src_h')}</h3>
              <ol>
                {turn.sources.map((s) => (
                  <li key={`${s.n}-${s.file}`}>
                    <span className="srcfile">{`[${s.n}] ${sourceLabel(s.file, s.paragraphs, t, lang, s.pages ?? [])}`}</span>
                    {s.quote ? <blockquote className="quote">{redact(s.quote)}</blockquote> : null}
                  </li>
                ))}
              </ol>
            </section>
          ) : null}
          {turn.details?.partial === true ? <p className="small warnline">{t('ask_partial')}</p> : null}
          <button type="button" className="greyline" aria-expanded={p.detailsOpen} aria-controls={p.inlineDetails && p.detailsOpen ? detailsId : undefined} onClick={p.onDetails} data-testid="grey-line">
            {line}
            <span className="sr">{` · ${t('details')}`}</span>
          </button>
          {p.inlineDetails && p.detailsOpen ? (
            <div id={detailsId} className="techline">
              <AnswerDetails turn={turn} />
            </div>
          ) : null}
          <div className="acts">
            <OwnApp apps={turn.handoff?.apps} />
            <button
              type="button"
              className="btn link small"
              data-testid="make-chore"
              onClick={() => {
                setChoreDraft(turn.question);
                go('chores');
              }}
            >
              <Icon name="chores" />
              {t('ask_make_chore')}
            </button>
          </div>
        </>
      );
      break;
    }
    case 'choices':
      body = (
        <div className="cardx choices" data-testid="choices-box">
          <h3>{t(reasonKey(turn.reason))}</h3>
          <ChoiceButtons turn={turn} onChoose={p.onChoose} busy={p.busy} />
        </div>
      );
      break;
    case 'over_limit': {
      const o = turn.over_limit;
      body = o ? (
        <div className="cardx choices" data-testid="over-limit">
          <h3>{t('ol_t', localMoney(o.over, currency, lang, t), localMoney(o.limit, currency, lang, t))}</h3>
          <p className="small muted">{t('ol_b', localMoney(o.spent, currency, lang, t), localMoney(o.pay, currency, lang, t))}</p>
          <button type="button" className="btn primary block" data-testid="raise-today" disabled={p.busy} onClick={p.onRaise}>
            {t('ol_raise', localMoney(o.raise_today.to, currency, lang, t))}
          </button>
          <button type="button" className="btn sec block" disabled={p.busy} onClick={p.onDeclineRaise}>
            {t('not_now')}
          </button>
        </div>
      ) : null;
      break;
    }
    case 'pending':
      body = (
        <p className="upd" role="status">
          <span className="spin-s" aria-hidden="true" />
          {t('ask_reading')}
        </p>
      );
      break;
    case 'queued': {
      const at = turn.until ? Date.parse(turn.until) : NaN;
      body = (
        <p className="okline" role="status" data-testid="queued">
          {Number.isFinite(at) ? t('ask_queued_at', fmtTime(at, lang)) : t('ask_queued')}
        </p>
      );
      break;
    }
    case 'handed_off':
      body = (
        <p className="okline" data-testid="handed-off">
          {t('ch_r_open', redact(turn.app?.name ?? ''))}
        </p>
      );
      break;
    default: {
      if (turn.cut_short) {
        body = <CutShort turn={turn} busy={p.busy} onContinue={p.onContinue} />;
        break;
      }
      const priv = turn.reason === 'private_no_service';
      body = (
        <ErrorBox
          icon={priv ? 'lock' : 'alert'}
          kind={priv ? 'warn' : 'bad'}
          what={t(reasonKey(turn.reason))}
          will={t('gl_nothing')}
          testId="ask-failed"
          actions={
            priv ? (
              <>
                {p.localRuntime ? (
                  <button type="button" className="btn sec small" onClick={p.onUseLocal} data-testid="use-local">
                    {t('ask_use_local')}
                  </button>
                ) : null}
                <button type="button" className="btn sec small" onClick={p.onChangeBadge}>
                  {t('ask_change_badge')}
                </button>
              </>
            ) : turn.choices ? (
              <ChoiceButtons turn={turn} onChoose={p.onChoose} busy={p.busy} />
            ) : undefined
          }
        />
      );
    }
  }
  return (
    <li className="turn" data-testid="turn" data-status={turn.status}>
      <div className="q">
        <p>{redact(turn.question)}</p>
        <span className="qmeta small muted">
          {turn.privacy && turn.privacy in BADGE_KEY ? t(BADGE_KEY[turn.privacy as Badge]) : null}
          {turn.attachments.length > 0 ? ` · ${t('ask_with_folders', fmtList(turn.attachments.map(redact), lang))}` : null}
        </span>
      </div>
      <div className="a">{body}</div>
    </li>
  );
}

// ---- the input

interface InputProps {
  folders: string[] | null;
  privacy: Badge;
  setPrivacy: (b: Badge) => void;
  attachments: string[];
  setAttachments: (a: string[]) => void;
  onSend: (text: string) => void;
  sending: boolean;
  badgeRef: RefObject<HTMLSelectElement | null>;
  paused: boolean;
  noPrivate: boolean;
}

export function AskInput(p: InputProps) {
  const { t, lang } = useI18n();
  const [text, setText] = useState('');
  const inputId = useId();
  const badgeId = useId();
  const submit = (e: FormEvent) => {
    e.preventDefault();
    const v = text.trim();
    if (!v || p.sending || p.paused) return;
    p.onSend(v);
    setText('');
  };
  return (
    <form className="askbar card" onSubmit={submit} data-testid="ask-form">
      <label htmlFor={inputId} className="sr">
        {t('ask_input')}
      </label>
      <textarea id={inputId} rows={2} value={text} placeholder={t('ask_ph')} onChange={(e) => setText(e.target.value)} data-testid="ask-input" maxLength={20000} />
      <div className="askopts">
        <label htmlFor={badgeId} className="small muted">
          {t('ask_privacy')}
        </label>
        <select id={badgeId} ref={p.badgeRef} value={p.privacy} onChange={(e) => p.setPrivacy(e.target.value as Badge)} data-testid="privacy">
          {BADGES.map((b) => (
            <option key={b} value={b}>
              {t(BADGE_KEY[b])}
            </option>
          ))}
        </select>
        <details className="attach" data-testid="attach">
          <summary className="btn sec small">
            <Icon name="records" />
            {p.attachments.length > 0 ? t('ask_attached_n', fmtNumber(p.attachments.length, lang)) : t('ask_attach')}
          </summary>
          <div className="attachlist card">
            {p.folders === null ? (
              <p className="small muted">{t('updating')}</p>
            ) : p.folders.length === 0 ? (
              <p className="small muted">{t('ask_no_folders')}</p>
            ) : (
              <fieldset>
                <legend className="small muted">{t('ask_attach_h')}</legend>
                {p.folders.map((f) => (
                  <label key={f} className="radio">
                    <input
                      type="checkbox"
                      checked={p.attachments.includes(f)}
                      onChange={(e) => p.setAttachments(e.target.checked ? [...p.attachments, f] : p.attachments.filter((x) => x !== f))}
                    />
                    {redact(f)}
                  </label>
                ))}
              </fieldset>
            )}
          </div>
        </details>
        <button type="submit" className="btn primary small send" disabled={p.sending || !text.trim() || p.paused} data-testid="ask-send">
          {t('ask_send')}
        </button>
      </div>
      {p.privacy === 'private' && p.noPrivate ? <p className="small warnline" data-testid="no-private">{t('rs_private')}</p> : null}
      {p.attachments.length > 0 ? <p className="small muted">{t('ask_files_note', fmtList(p.attachments.map(redact), lang))}</p> : null}
    </form>
  );
}

// ---- the screen

export function Ask() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const desktop = useIsDesktop();
  const paused = isPaused(state);
  const convs = useLoad(useCallback(() => client.conversations(), [client]));
  const folders = useLoad(useCallback(() => client.folders(), [client]));
  const services = useLoad(useCallback(() => client.services(), [client]));
  const [current, setCurrent] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [privacy, setPrivacy] = useState<Badge>(() => {
    const d = state.status?.default_privacy;
    return d === 'local' || d === 'private' ? d : 'standard';
  });
  const [attachments, setAttachments] = useState<string[]>([]);
  const [sending, setSending] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [outbox, setOutbox] = useState<Outbox | null>(null);
  const [problem, setProblem] = useState<Key | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  const [showList, setShowList] = useState(false);
  const badgeRef = useRef<HTMLSelectElement>(null);
  const deleteDialog = useRef<HTMLDialogElement>(null);
  const deleteTitle = useId();
  const endRef = useRef<HTMLDivElement>(null);
  const localRuntime = state.status?.local_runtime === true;
  const noPrivate = services.data !== null && !services.data.connected.some((s) => s.badge === 'private' || s.badge === 'local');

  const open = useCallback(
    async (id: string | null) => {
      setCurrent(id);
      setDetails(null);
      setShowList(false);
      setProblem(null);
      if (id === null) {
        setTurns([]);
        return;
      }
      try {
        setTurns(await client.conversation(id));
      } catch {
        setProblem('ask_load_failed');
      }
    },
    [client],
  );

  const replace = (turn: Turn) => setTurns((ts) => ts.map((x) => (x.id === turn.id ? turn : x)));
  const reloadConvs = convs.reload;

  const send = useCallback(
    async (o: Outbox) => {
      setSending(o.text);
      setProblem(null);
      try {
        const reply = await client.ask({ text: o.text, conversation_id: o.conversationId, privacy: o.privacy, attachments: o.attachments });
        setOutbox(null);
        setCurrent(reply.conversation_id);
        setTurns((ts) => (o.conversationId === reply.conversation_id ? [...ts, reply.answer] : [reply.answer]));
        void reloadConvs();
      } catch (err) {
        if (err instanceof HomeError && err.kind === 'unreachable') setOutbox(o);
        else setProblem(err instanceof HomeError && err.code === 'invalid_request' ? 'ask_invalid' : 'ask_failed');
      } finally {
        setSending(null);
      }
    },
    [client, reloadConvs],
  );

  // Offline: the question waits here and is asked when the home answers again (section 4.2).
  const homeBack = state.problem === null && state.loaded;
  useEffect(() => {
    if (!outbox || sending || !homeBack) return;
    const timer = setTimeout(() => void send(outbox), 0);
    return () => clearTimeout(timer);
  }, [outbox, sending, homeBack, send]);

  // An answer still on its way (files being read, a question waiting for the refill): look again.
  useEffect(() => {
    const waiting = turns.filter((x) => x.status === 'pending' || x.status === 'queued');
    if (waiting.length === 0) return;
    const ms = waiting.some((x) => x.status === 'pending') ? PENDING_POLL_MS : QUEUED_POLL_MS;
    const timer = setTimeout(() => {
      for (const w of waiting) void client.askGet(w.id).then((r) => replace(r.answer), () => undefined);
    }, ms);
    return () => clearTimeout(timer);
  }, [turns, client]);

  useEffect(() => {
    endRef.current?.scrollIntoView?.({ block: 'end' });
  }, [turns.length, sending]);

  const choose = async (turn: Turn, c: OrderedChoice, raise = false) => {
    setBusy(true);
    setProblem(null);
    try {
      if (c.kind === 'handoff') {
        // Open the person's own app first: what they type there is theirs. Then the home records that it was handed off.
        const ok = await openExternal(c.url);
        if (!ok) {
          setProblem('ask_open_failed');
          return;
        }
        replace((await client.choose(turn.id, 'handoff', { app: c.app.id })).answer);
      } else {
        replace((await client.choose(turn.id, c.kind, raise ? { raiseToday: true } : {})).answer);
      }
    } catch {
      setProblem('ask_failed');
    } finally {
      setBusy(false);
    }
  };

  const continueTurn = async (turn: Turn) => {
    setBusy(true);
    setProblem(null);
    try {
      replace((await client.continueAsk(turn.id)).answer);
    } catch {
      setProblem('ask_failed');
    } finally {
      setBusy(false);
    }
  };

  const detailTurn = details ? (turns.find((x) => x.id === details) ?? null) : null;
  const list = convs.data ?? [];

  const listPane = (
    <nav className="card convlist" aria-label={t('ask_list')} data-testid="conversations">
      <button type="button" className="btn sec small block" onClick={() => void open(null)} data-testid="new-conversation">
        <Icon name="ask" />
        {t('ask_new')}
      </button>
      {list.length === 0 ? (
        <p className="small muted">{t('ask_list_empty')}</p>
      ) : (
        <ul>
          {list.map((c) => (
            <li key={c.id}>
              <button type="button" className="ln" aria-current={c.id === current ? 'true' : undefined} onClick={() => void open(c.id)}>
                <span className="sp">{redact(c.preview)}</span>
                <span className="small muted">{fmtDate(Date.parse(c.updated_at), lang)}</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </nav>
  );

  // The services this conversation's questions went to: deleting it here does not delete what they kept.
  const servicesUsed = [...new Set(turns.map((x) => (x.route && !x.route.local && !x.route.cache ? x.route.name : null)).filter((n): n is string => !!n).map(redact))];

  const deleteCurrent = async () => {
    if (!current) return;
    try {
      await client.deleteConversation(current);
      await open(null);
      void convs.reload();
    } catch {
      setProblem('ask_failed');
    }
  };

  const conversation = (
    <section className="convo" aria-label={t('ask_conversation')} data-testid="conversation">
      {!desktop ? (
        <div className="row convtop">
          <button type="button" className="btn sec small" aria-expanded={showList} onClick={() => setShowList(!showList)} data-testid="show-conversations">
            {t('ask_list_n', fmtNumber(list.length, lang))}
          </button>
          <span className="sp" />
          {current ? (
            <button type="button" className="btn link small" onClick={() => void open(null)}>
              {t('ask_new')}
            </button>
          ) : null}
        </div>
      ) : null}
      {!desktop && showList ? listPane : null}
      {paused ? <p className="small muted">{t('ask_paused')}</p> : null}
      {turns.length === 0 && !sending ? <EmptyBox icon="ask" title={t('ask_empty_t')} body={t('ask_start_b')} testId="ask-empty" /> : null}
      <ol className="turns">
        {turns.map((turn) => (
          <TurnView
            key={turn.id}
            turn={turn}
            now={state.now}
            busy={busy}
            localRuntime={localRuntime}
            detailsOpen={details === turn.id}
            inlineDetails={!desktop}
            onDetails={() => setDetails(details === turn.id ? null : turn.id)}
            onChoose={(c) => void choose(turn, c)}
            onRaise={() => {
              const pay = orderedChoices(turn.choices).find((c) => c.kind === 'pay');
              if (pay) void choose(turn, pay, true);
            }}
            onDeclineRaise={() => replace({ ...turn, status: 'choices' })}
            onUseLocal={() => {
              setPrivacy('local');
              void send({ text: turn.question, privacy: 'local', attachments: turn.attachments, conversationId: current });
            }}
            onChangeBadge={() => badgeRef.current?.focus()}
            onContinue={() => void continueTurn(turn)}
          />
        ))}
        {sending ? (
          <li className="turn" data-testid="sending">
            <div className="q">
              <p>{redact(sending)}</p>
            </div>
            <div className="a">
              <p className="upd" role="status">
                <span className="spin-s" aria-hidden="true" />
                {t('ask_thinking')}
              </p>
            </div>
          </li>
        ) : null}
      </ol>
      {outbox ? <ErrorBox icon="cloud" kind="info" what={t('ask_offline')} will={redact(outbox.text)} testId="ask-offline" /> : null}
      {problem ? <ErrorBox icon="alert" what={t(problem)} will={t('gl_nothing')} testId="ask-problem" /> : null}
      <div ref={endRef} />
      <AskInput
        folders={folders.data}
        privacy={privacy}
        setPrivacy={setPrivacy}
        attachments={attachments}
        setAttachments={setAttachments}
        sending={sending !== null}
        paused={paused}
        noPrivate={noPrivate}
        badgeRef={badgeRef}
        onSend={(text) => void send({ text, privacy, attachments, conversationId: current })}
      />
      {current ? (
        <button type="button" className="btn link small" onClick={() => deleteDialog.current?.showModal()} data-testid="delete-conversation">
          {t('ask_delete')}
        </button>
      ) : null}
      <dialog ref={deleteDialog} className="sheet" aria-labelledby={deleteTitle}>
        <h2 id={deleteTitle}>{t('ask_delete_q')}</h2>
        <p>{servicesUsed.length ? t('ask_delete_b', fmtList(servicesUsed, lang)) : t('ask_delete_b_none')}</p>
        <div className="btnrow">
          <button
            type="button"
            className="btn primary"
            data-testid="delete-confirm"
            onClick={() => {
              deleteDialog.current?.close();
              void deleteCurrent();
            }}
          >
            {t('ask_delete')}
          </button>
          <button type="button" className="btn sec" onClick={() => deleteDialog.current?.close()}>
            {t('cancel')}
          </button>
        </div>
      </dialog>
    </section>
  );

  return (
    <div className={desktop ? `askgrid${detailTurn ? ' withdetails' : ''}` : 'col'} data-testid="screen-ask">
      {desktop ? listPane : null}
      {conversation}
      {desktop && detailTurn ? (
        <aside className="card detailspane" aria-label={t('details')} data-testid="details-pane">
          <div className="row">
            <h2 className="sp">{t('details')}</h2>
            <button type="button" className="icon-btn" aria-label={t('details_hide')} onClick={() => setDetails(null)}>
              <Icon name="x" />
            </button>
          </div>
          <AnswerDetails turn={detailTurn} />
        </aside>
      ) : null}
    </div>
  );
}
