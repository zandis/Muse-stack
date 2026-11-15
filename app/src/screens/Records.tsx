// Records (docs/spec/02-ux.md section 4.4): what did my helper do, and can I prove it?
// Every record as one plain sentence, newest first, grouped by day; filters; the whole
// record with its proof; "Check my records" (the home checks every record on each
// call); Export. The sentence is written here from the record's facts, in the person's
// language; what the home itself wrote is shown in the record, or said to be removed.
import { useCallback, useEffect, useId, useMemo, useRef, useState } from 'react';
import { Icon } from '../components/Icon';
import { useIsDesktop } from '../components/Shell';
import { EmptyBox, ErrorBox, Tag, Updating } from '../components/ui';
import { HomeError } from '../home/client';
import type { HomeRecord, Permission, RecordsPage, RecordsVerify } from '../home/types';
import { useI18n, type Key } from '../i18n';
import { dayKey, fmtLongDate, fmtNumber, fmtTime } from '../i18n/format';
import { redact } from '../lib/redact';
import { Link, useRouter } from '../router';
import { useAppClient, useLoad } from '../state/app-client';
import { isPaused } from '../state/reducer';
import { useAppState } from '../state/store';
import {
  BADGE_WORD,
  changedSomething,
  costWords,
  dateWords,
  dayHeading,
  permissionSentence,
  recordSentence,
  subjectOf,
  underOf,
  whereOf,
  type ServiceInfo,
  type WordsContext,
} from './trust-words';

const PAGE = 100;
type When = 'all' | 'today' | '7' | '30';
type Chip = 'left' | 'cost' | 'changed' | 'helper';
const CHIPS: [Chip, Key][] = [
  ['left', 'rc_f_left'],
  ['cost', 'rc_f_cost'],
  ['changed', 'rc_f_changed'],
  ['helper', 'rc_f_helper'],
];

/** The record number people see: the home's sequence number, counted from 1. */
export function recordNumber(r: { seq: number }): number {
  return r.seq + 1;
}

export type Proof = 'sealed' | 'unsealed' | 'bad' | 'after' | 'unknown';

/** Where one record stands, from the home's check of the whole chain and its last seal. */
export function proofOf(r: HomeRecord, verify: RecordsVerify, sealedSize: number | null): Proof {
  if (!verify.ok) {
    if (typeof verify.index !== 'number') return 'unknown';
    if (r.seq === verify.index) return 'bad';
    if (r.seq > verify.index) return 'after';
  }
  return sealedSize !== null && r.seq < sealedSize ? 'sealed' : 'unsealed';
}

function kindKey(verify: RecordsVerify): Key {
  switch (verify.kind) {
    case 'summary_mismatch':
      return 'rc_kind_summary_mismatch';
    case 'bad_signature':
      return 'rc_kind_bad_signature';
    case 'missing':
      return 'rc_kind_missing';
    case 'size_mismatch':
    case 'root_mismatch':
    case 'bad_checkpoint':
      return 'rc_kind_sealed';
    default:
      return 'rc_kind_other';
  }
}

function useWords(): WordsContext {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const { status } = useAppState();
  const perms = useLoad(useCallback(() => client.permissions(lang), [client, lang]));
  const services = useLoad(useCallback(() => client.services(), [client]));
  return useMemo(() => {
    const svc = new Map<string, ServiceInfo>();
    for (const p of status?.providers ?? []) svc.set(p.id, { name: p.name, badge: null });
    for (const s of services.data?.connected ?? []) svc.set(s.id, { name: s.name, badge: s.badge });
    return {
      t,
      lang,
      permissions: perms.data ? new Map(perms.data.mandates.map((p) => [p.id, p] as [string, Permission])) : perms.problem ? null : new Map(),
      services: svc,
      money: status?.money,
    };
  }, [t, lang, perms.data, perms.problem, services.data, status]);
}

// ---- the seal line and the check

function ProofCard({ page, onChecked }: { page: RecordsPage; onChecked: (v: RecordsVerify) => void }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const [open, setOpen] = useState(false);
  const [why, setWhy] = useState(false);
  const [checking, setChecking] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const v = page.verify;
  const sealedAt = page.sealed ? Date.parse(page.sealed.time) : NaN;
  const check = async () => {
    setChecking(true);
    setNote(null);
    try {
      const fresh = await client.records({ limit: 1 });
      onChecked(fresh.verify);
      const fv = fresh.verify;
      setNote(fv.ok ? t('rc_checked_ok', fmtNumber(fv.size, lang)) : typeof fv.index === 'number' ? t('rc_checked_bad', fmtNumber(fv.index + 1, lang)) : t('rc_checked_bad_any'));
    } catch {
      setNote(t('rc_problem'));
    } finally {
      setChecking(false);
    }
  };
  return (
    <section className="card stack" aria-label={t('rc_d_proof')} data-testid="records-proof">
      {v.ok ? (
        <p className="row okline" data-testid="records-ok">
          <Icon name="permissions" />
          <b>{v.size === 1 ? t('rc_ok_one') : t('rc_ok', fmtNumber(v.size, lang))}</b>
        </p>
      ) : (
        <div className="errc bad inline" data-testid="records-bad">
          <span className="eic">
            <Icon name="alert" />
          </span>
          <div className="errbody">
            <b>{typeof v.index === 'number' ? t('rc_bad', fmtNumber(v.index + 1, lang)) : t('rc_bad_any')}</b>
            <span className="small">{t(kindKey(v))}</span>
            <span className="small muted">{t('rc_bad_w')}</span>
            <div className="acts">
              <button type="button" className="btn sec small" aria-expanded={why} onClick={() => setWhy(!why)}>
                {t('rc_bad_btn')}
              </button>
            </div>
            {why ? <p className="small muted">{t('rc_bad_x')}</p> : null}
          </div>
        </div>
      )}
      <div className="row wrap">
        <Icon name="lock" />
        <span className="sp">{Number.isFinite(sealedAt) ? t('rc_sealed', `${dateWords(sealedAt, state.now, lang)} ${fmtTime(sealedAt, lang)}`) : t('rc_not_sealed')}</span>
        {page.sealed ? (
          <button type="button" className="btn link small" aria-expanded={open} onClick={() => setOpen(!open)}>
            {t(open ? 'details_hide' : 'details')}
          </button>
        ) : null}
      </div>
      {open && page.sealed ? (
        <dl className="kv small" data-testid="seal-details">
          <div>
            <dt>{t('rc_seal_what')}</dt>
            <dd>{t('rc_seal_what_v')}</dd>
          </div>
          <div>
            <dt>{t('rc_seal_n')}</dt>
            <dd>{fmtNumber(page.sealed.size, lang)}</dd>
          </div>
          <div>
            <dt>{t('rc_seal_fp')}</dt>
            <dd className="mono">{`${page.sealed.root.slice(0, 16)}…`}</dd>
          </div>
          {page.device ? (
            <div>
              <dt>{t('rc_home_id')}</dt>
              <dd className="mono">{`${page.device.slice(0, 16)}…`}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      <div className="row wrap">
        <button type="button" className="btn sec small" onClick={() => void check()} disabled={checking} data-testid="records-check">
          <Icon name="permissions" />
          {t('rc_check')}
        </button>
        <span className="small" role="status" data-testid="records-check-note">
          {checking ? t('rc_checking') : note}
        </span>
      </div>
    </section>
  );
}

// ---- export

function ExportButton() {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const dialog = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLButtonElement>(null);
  const title = useId();
  const [redactText, setRedactText] = useState(false);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState<{ text: string; file: string; device: string } | null>(null);
  const [error, setError] = useState<Key | null>(null);
  const [how, setHow] = useState(false);
  const close = () => {
    dialog.current?.close();
    opener.current?.focus();
  };
  const go = async () => {
    setBusy(true);
    setError(null);
    setDone(null);
    try {
      const got = await client.exportRecords(redactText);
      saveFile(got.file_name, `${JSON.stringify(got.export)}\n`);
      setDone({
        text: got.redacted > 0 ? t('rc_export_done_redacted', got.file_name, fmtNumber(got.count, lang)) : t('rc_export_done', got.file_name, fmtNumber(got.count, lang)),
        file: got.file_name,
        device: got.device,
      });
    } catch (err) {
      const conflict = err instanceof HomeError && err.code === 'conflict';
      setError(conflict ? 'rc_export_bad' : 'rc_export_failed');
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        ref={opener}
        className="btn sec small"
        onClick={() => {
          setDone(null);
          setError(null);
          dialog.current?.showModal();
        }}
        data-testid="records-export"
      >
        <Icon name="download" />
        {t('rc_export')}
      </button>
      <dialog ref={dialog} className="sheet" aria-labelledby={title} data-testid="export-dialog" onCancel={() => opener.current?.focus()}>
        <h2 id={title}>{t('rc_export_t')}</h2>
        <p>{t('rc_export_b')}</p>
        <label className="row chk">
          <input type="checkbox" checked={redactText} onChange={(e) => setRedactText(e.target.checked)} data-testid="export-redact" />
          <span>{t('rc_export_redact')}</span>
        </label>
        <p className="small" role="status" data-testid="export-note">
          {busy ? t('rc_export_busy') : done ? done.text : error ? t(error) : null}
        </p>
        {done ? (
          <>
            <button type="button" className="btn link small" aria-expanded={how} onClick={() => setHow(!how)}>
              {t('rc_export_how')}
            </button>
            {how ? (
              <div className="small muted stack">
                <p>{t('rc_export_how_v', done.file)}</p>
                <p className="mono">{`${t('rc_home_id')}: ${done.device}`}</p>
              </div>
            ) : null}
          </>
        ) : null}
        <div className="btnrow">
          {done ? null : (
            <button type="button" className="btn primary" onClick={() => void go()} disabled={busy} data-testid="export-go">
              <Icon name="download" />
              {t('rc_export_go')}
            </button>
          )}
          <button type="button" className="btn sec" onClick={close}>
            {t(done ? 'rc_d_close' : 'cancel')}
          </button>
        </div>
      </dialog>
    </>
  );
}

/** Hand the person a file. In a browser this is a download; nothing leaves the device. */
function saveFile(name: string, text: string): void {
  if (typeof URL.createObjectURL !== 'function') return;
  const url = URL.createObjectURL(new Blob([text], { type: 'application/json' }));
  const a = document.createElement('a');
  a.href = url;
  a.download = name;
  a.rel = 'noopener';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1000);
}

// ---- one record

function RecordDetail({ r, page, words, onClose }: { r: HomeRecord; page: RecordsPage; words: WordsContext; onClose?: () => void }) {
  const { t, lang } = useI18n();
  const state = useAppState();
  const [open, setOpen] = useState(false);
  const cost = costWords(r, words);
  const where = whereOf(r, words);
  const proof = proofOf(r, page.verify, page.sealed?.size ?? null);
  const sealedAt = page.sealed ? Date.parse(page.sealed.time) : NaN;
  const proofText: Record<Proof, string> = {
    sealed: t('rc_p_sealed', Number.isFinite(sealedAt) ? dateWords(sealedAt, state.now, lang) : ''),
    unsealed: t('rc_p_unsealed'),
    bad: t('rc_p_bad'),
    after: t('rc_p_after'),
    unknown: t('rc_p_unknown'),
  };
  const perm = r.mandate && words.permissions?.get(r.mandate);
  const rows: [Key, string][] = [
    ['rc_d_when', `${fmtLongDate(r.time, lang)} ${fmtTime(r.time, lang)}`],
    ['rc_d_who', subjectOf(r, words)],
    ['rc_d_perm', underOf(r, words)],
    ['rc_d_cost', cost.main],
    ['rc_d_where', where.text],
  ];
  if (where.badge !== undefined) rows.push(['rc_d_privacy', where.badge ? t(BADGE_WORD[where.badge]) : t('rc_e_badge_unknown')]);
  return (
    <div className="stack" data-testid="record-detail" data-seq={r.seq}>
      <h2 className="rdh">{recordSentence(r, words)}</h2>
      <dl className="kv">
        {rows.map(([k, v]) => (
          <div key={k}>
            <dt>{t(k)}</dt>
            <dd>{v}</dd>
          </div>
        ))}
        <div>
          <dt>{t('rc_d_said')}</dt>
          <dd data-testid="record-said">{r.summary !== undefined ? redact(r.summary) : <i>{t('rc_d_redacted')}</i>}</dd>
        </div>
        <div>
          <dt>{t('rc_d_proof')}</dt>
          <dd className={proof === 'bad' || proof === 'after' || proof === 'unknown' ? 'badline' : 'okline'} data-testid="record-proof">
            {proofText[proof]}
          </dd>
        </div>
      </dl>
      {perm ? (
        <Link to="permissions" className="btn link small">
          {t('rc_d_see_perm')}
        </Link>
      ) : null}
      <button type="button" className="btn link small" aria-expanded={open} onClick={() => setOpen(!open)}>
        {t(open ? 'details_hide' : 'details')}
      </button>
      {open ? (
        <dl className="kv small" data-testid="record-more">
          <div>
            <dt>{t('rc_d_number')}</dt>
            <dd>{fmtNumber(recordNumber(r), lang)}</dd>
          </div>
          {cost.usd ? (
            <div>
              <dt>{t('rc_d_usd')}</dt>
              <dd>{cost.usd}</dd>
            </div>
          ) : null}
          <div>
            <dt>{t('dt_rc_action')}</dt>
            <dd className="mono">{r.action}</dd>
          </div>
          <div>
            <dt>{t('dt_rc_device')}</dt>
            <dd className="mono">{`${r.device.slice(0, 16)}…`}</dd>
          </div>
          {r.links?.card ? (
            <div>
              <dt>{t('dt_rc_card')}</dt>
              <dd className="mono">{redact(r.links.card)}</dd>
            </div>
          ) : null}
          {r.links?.job ? (
            <div>
              <dt>{t('dt_rc_job')}</dt>
              <dd className="mono">{redact(r.links.job)}</dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {onClose ? (
        <button type="button" className="btn sec block" onClick={onClose} data-testid="record-close">
          {t('rc_d_close')}
        </button>
      ) : null}
    </div>
  );
}

function Tags({ r, words, proof }: { r: HomeRecord; words: WordsContext; proof: Proof }) {
  const { t } = useI18n();
  const cost = costWords(r, words);
  const where = whereOf(r, words);
  return (
    <span className="row wrap g6">
      {r.cost && r.cost.minor > 0 ? <Tag kind="warn" icon="wallet">{cost.main}</Tag> : <Tag icon="check">{cost.main}</Tag>}
      {where.left ? <Tag icon="external">{where.badge ? `${where.text} · ${t(BADGE_WORD[where.badge])}` : where.text}</Tag> : where.left === false ? <Tag kind="ok" icon="lock">{where.text}</Tag> : <Tag>{where.text}</Tag>}
      {r.summary === undefined ? <Tag icon="info">{t('rc_tag_redacted')}</Tag> : null}
      {proof === 'bad' ? <Tag kind="bad" icon="alert">{t('rc_tag_bad')}</Tag> : proof === 'after' || proof === 'unknown' ? <Tag kind="warn">{t('rc_tag_unconfirmed')}</Tag> : null}
    </span>
  );
}

// ---- the screen

function whoValue(r: HomeRecord, words: WordsContext): string {
  if (r.actor === 'person') return 'you';
  const subject = subjectOf(r, words);
  return subject === words.t('rc_s_helper') || subject === words.t('rc_s_muse') ? 'helper' : `p:${subject}`;
}

function RecordsBody({ mandate }: { mandate: string | null }) {
  const { t, lang } = useI18n();
  const client = useAppClient();
  const state = useAppState();
  const desktop = useIsDesktop();
  const { go } = useRouter();
  const words = useWords();
  const paused = isPaused(state);
  const first = useLoad(useCallback(() => client.records({ limit: PAGE, ...(mandate ? { mandate } : {}) }), [client, mandate]));
  const [older, setOlder] = useState<HomeRecord[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [verify, setVerify] = useState<RecordsVerify | null>(null);
  const [loadingMore, setLoadingMore] = useState(false);
  const [chips, setChips] = useState<Chip[]>([]);
  const [who, setWho] = useState('all');
  const [when, setWhen] = useState<When>('all');
  const [selected, setSelected] = useState<number | null>(null);
  const sheet = useRef<HTMLDialogElement>(null);
  const opener = useRef<HTMLElement | null>(null);
  const sheetTitle = useId();

  useEffect(() => {
    if (first.data) {
      setOlder([]);
      setNext(first.data.next_before);
      setVerify(null);
    }
  }, [first.data]);

  const page: RecordsPage | null = first.data ? { ...first.data, verify: verify ?? first.data.verify } : null;
  const all = useMemo(() => (first.data ? [...first.data.records, ...older] : []), [first.data, older]);
  const since = when === 'all' ? null : when === 'today' ? dayKey(state.now) : state.now - Number(when) * 86_400_000;
  const shown = all.filter((r) => {
    if (chips.includes('left') && !whereOf(r, words).left) return false;
    if (chips.includes('cost') && !(r.cost && r.cost.minor > 0)) return false;
    if (chips.includes('changed') && !changedSomething(r)) return false;
    if (chips.includes('helper') && r.actor !== 'helper') return false;
    if (who !== 'all' && whoValue(r, words) !== who) return false;
    if (typeof since === 'string' && dayKey(r.time) !== since) return false;
    if (typeof since === 'number' && r.time < since) return false;
    return true;
  });
  const programs = [...new Set(all.map((r) => whoValue(r, words)).filter((v) => v.startsWith('p:')))];
  const chosen = all.find((r) => r.seq === selected) ?? null;
  const permission = mandate ? words.permissions?.get(mandate) : undefined;

  const more = async () => {
    if (next === null) return;
    setLoadingMore(true);
    try {
      const got = await client.records({ limit: PAGE, before: next, ...(mandate ? { mandate } : {}) });
      setOlder((o) => [...o, ...got.records]);
      setNext(got.next_before);
    } catch {
      /* the button stays; the list keeps what it has */
    } finally {
      setLoadingMore(false);
    }
  };
  const open = (r: HomeRecord, el: HTMLElement) => {
    opener.current = el;
    setSelected(r.seq);
    if (!desktop) setTimeout(() => sheet.current?.showModal(), 0);
  };
  const closeSheet = () => {
    sheet.current?.close();
    opener.current?.focus();
  };

  // Group by local day, newest first (the home already sends them newest first).
  const groups: { key: string; label: string; rows: HomeRecord[] }[] = [];
  for (const r of shown) {
    const key = dayKey(r.time);
    const last = groups[groups.length - 1];
    if (last && last.key === key) last.rows.push(r);
    else groups.push({ key, label: dayHeading(r.time, state.now, lang, t), rows: [r] });
  }

  const tools = (
    <div className="rtools" data-testid="records-tools">
      <div className="chips" role="group" aria-label={t('rc_filters')}>
        {CHIPS.map(([k, label]) => (
          <button key={k} type="button" className="chip" aria-pressed={chips.includes(k)} onClick={() => setChips(chips.includes(k) ? chips.filter((c) => c !== k) : [...chips, k])} data-testid={`chip-${k}`}>
            {t(label)}
          </button>
        ))}
      </div>
      <div className="row wrap g6">
        <label className="row g6 small">
          <span>{t('rc_who')}</span>
          <select value={who} onChange={(e) => setWho(e.target.value)} data-testid="filter-who">
            <option value="all">{t('rc_who_all')}</option>
            <option value="you">{t('rc_who_you')}</option>
            <option value="helper">{t('rc_who_helper')}</option>
            {programs.map((p) => (
              <option key={p} value={p}>
                {p.slice(2)}
              </option>
            ))}
          </select>
        </label>
        <label className="row g6 small">
          <span>{t('rc_when')}</span>
          <select value={when} onChange={(e) => setWhen(e.target.value as When)} data-testid="filter-when">
            <option value="all">{t('rc_when_all')}</option>
            <option value="today">{t('rc_when_today')}</option>
            <option value="7">{t('rc_when_7')}</option>
            <option value="30">{t('rc_when_30')}</option>
          </select>
        </label>
        <ExportButton />
      </div>
    </div>
  );

  let list;
  if (page && all.length === 0) {
    list = mandate ? (
      <p className="card muted" data-testid="records-none">
        {t('rc_none_perm')}
      </p>
    ) : (
      <EmptyBox
        icon="records"
        title={t('re_t')}
        body={t('re_b')}
        testId="records-empty"
        action={
          <Link to="ask" className="btn primary">
            {t('re_btn')}
          </Link>
        }
      />
    );
  } else if (page && shown.length === 0) {
    list = (
      <p className="card muted" data-testid="records-none">
        {t('rc_none')}
      </p>
    );
  } else if (page && desktop) {
    list = (
      <div className="card tblc">
        <table className="rt" data-testid="records-table" aria-label={t('rc_list')}>
          <thead>
            <tr>
              {(['rc_col_time', 'rc_col_what', 'rc_col_perm', 'rc_col_cost', 'rc_col_where'] as Key[]).map((k) => (
                <th key={k} scope="col">
                  {t(k)}
                </th>
              ))}
            </tr>
          </thead>
          {groups.map((g) => (
            <tbody key={g.key}>
              <tr className="dayrow">
                <th colSpan={5} scope="colgroup">
                  {g.label}
                </th>
              </tr>
              {g.rows.map((r) => {
                const proof = proofOf(r, page.verify, page.sealed?.size ?? null);
                const where = whereOf(r, words);
                return (
                  <tr key={r.seq} className={`${selected === r.seq ? 'sel' : ''}${proof === 'bad' ? ' badrow' : ''}`} data-testid="record-row" data-seq={r.seq}>
                    <td className="tm">{fmtTime(r.time, lang)}</td>
                    <td>
                      <button type="button" className="rlink" aria-pressed={selected === r.seq} onClick={(e) => open(r, e.currentTarget)}>
                        {recordSentence(r, words)}
                      </button>
                      {r.summary === undefined || proof === 'bad' || proof === 'after' ? (
                        <div>
                          {r.summary === undefined ? <Tag icon="info">{t('rc_tag_redacted')}</Tag> : null}
                          {proof === 'bad' ? (
                            <Tag kind="bad" icon="alert">
                              {t('rc_tag_bad')}
                            </Tag>
                          ) : proof === 'after' ? (
                            <Tag kind="warn">{t('rc_tag_unconfirmed')}</Tag>
                          ) : null}
                        </div>
                      ) : null}
                    </td>
                    <td>{underOf(r, words)}</td>
                    <td className="cost">{costWords(r, words).main}</td>
                    <td>
                      {where.text}
                      {where.badge ? <span className="small muted">{` · ${t(BADGE_WORD[where.badge])}`}</span> : null}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          ))}
        </table>
      </div>
    );
  } else if (page) {
    list = (
      <div className="stack" data-testid="records-list" aria-label={t('rc_list')} role="region">
        {groups.map((g) => (
          <section key={g.key} className="stack" aria-label={g.label}>
            <h3 className="logday">{g.label}</h3>
            <ul className="stack">
              {g.rows.map((r) => {
                const proof = proofOf(r, page.verify, page.sealed?.size ?? null);
                return (
                  <li key={r.seq}>
                    <button type="button" className={`logitem rbtn${proof === 'bad' ? ' badrow' : ''}`} onClick={(e) => open(r, e.currentTarget)} data-testid="record-row" data-seq={r.seq}>
                      <span className="tm">{fmtTime(r.time, lang)}</span>
                      <span className="tx">
                        <span className="w">{recordSentence(r, words)}</span>
                        <small>{`${t('cd_under')} ${underOf(r, words)}`}</small>
                        <Tags r={r} words={words} proof={proof} />
                      </span>
                    </button>
                  </li>
                );
              })}
            </ul>
          </section>
        ))}
      </div>
    );
  }

  return (
    <div className="col wide" data-testid="screen-records" data-loaded={page !== null}>
      {first.loading && page ? <Updating text={t('updating')} /> : null}
      {first.problem ? (
        <ErrorBox
          icon={first.problem === 'unreachable' ? 'cloud' : 'alert'}
          what={t('rc_problem')}
          will={page ? t('rc_problem_kept') : t('un_will')}
          testId="records-problem"
          actions={
            <button type="button" className="btn sec small" onClick={() => void first.reload()}>
              {t('retry')}
            </button>
          }
        />
      ) : null}
      {paused ? <ErrorBox icon="pause" kind="info" what={t('rc_paused')} testId="records-paused" /> : null}
      {page && (page.records.length > 0 || page.verify.size > 0) ? <ProofCard page={page} onChecked={setVerify} /> : null}
      {mandate ? (
        <div className="card row wrap" data-testid="records-under">
          <Icon name="permissions" />
          <span className="sp">{t('rc_under_filter', permission ? permissionSentence(permission, words, false) : t('rc_u_some'))}</span>
          <button type="button" className="btn link small" onClick={() => go('records', true, '')}>
            {t('rc_under_clear')}
          </button>
        </div>
      ) : null}
      {page && all.length > 0 ? tools : null}
      <div className={desktop ? 'split rsplit' : 'stack'}>
        <div className="stack">
          {list}
          {!page && !first.problem ? <div className="card skeleton" aria-hidden="true" /> : null}
          {page && next !== null ? (
            <button type="button" className="btn sec" onClick={() => void more()} disabled={loadingMore} data-testid="records-more">
              {t('rc_more')}
            </button>
          ) : null}
          {page && all.length > 0 ? <p className="small muted">{t('rc_count', fmtNumber(shown.length, lang))}</p> : null}
        </div>
        {desktop && page && all.length > 0 ? (
          <aside className="card stack pane" aria-label={t('rc_d_proof')} data-testid="record-pane">
            {chosen ? <RecordDetail r={chosen} page={page} words={words} /> : <p className="muted">{t('rc_d_pick')}</p>}
          </aside>
        ) : null}
      </div>
      {!desktop ? (
        <dialog ref={sheet} className="sheet" aria-labelledby={sheetTitle} onCancel={() => opener.current?.focus()} data-testid="record-sheet">
          <span id={sheetTitle} className="sr">
            {chosen ? t('rc_d_title', fmtNumber(recordNumber(chosen), lang)) : ''}
          </span>
          {chosen && page ? <RecordDetail r={chosen} page={page} words={words} onClose={closeSheet} /> : null}
        </dialog>
      ) : null}
    </div>
  );
}

export function Records() {
  const { search } = useRouter();
  const mandate = new URLSearchParams(search).get('permission');
  const valid = mandate && /^[A-Za-z0-9._:-]{1,128}$/.test(mandate) ? mandate : null;
  return <RecordsBody key={valid ?? 'all'} mandate={valid} />;
}
