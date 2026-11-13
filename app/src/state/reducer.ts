// The app's state and every way it changes. Pure: the effects (requests, timers,
// storage) live in store.tsx.
import type { HomeError } from '../home/client';
import type { Card, Decision, Job, Status } from '../home/types';

/** What is wrong with the connection to the home, in the terms the screens show (UX section 8). */
export type Problem = 'unreachable' | 'auth' | 'generic';

export type NoticeKind = 'approved' | 'approved_always' | 'declined' | 'card_answered' | 'card_expired' | 'card_failed' | 'pause_failed';

export interface Notice {
  seq: number;
  kind: NoticeKind;
}

export interface State {
  /** True once the home has answered at least once in this session. */
  loaded: boolean;
  refreshing: boolean;
  problem: Problem | null;
  status: Status | null;
  /** Waiting cards, newest first, as the home last sent them. */
  cards: Card[];
  jobs: Job[];
  /** When the home last answered, ms since the epoch. Survives a restart (kept in storage). */
  lastOkAt: number | null;
  /** The app's clock, moved forward at every round, so expiry times re-render. */
  now: number;
  /** Cards answered here and not yet confirmed by the home: hidden at once, put back if the answer fails. */
  answering: Record<string, Decision>;
  /** Pause pressed and not yet confirmed: shown at once, put back if it fails. */
  pausedOverride: boolean | null;
  notice: Notice | null;
  /** "Use this device for now" was pressed while the home was unreachable. */
  useThisDevice: boolean;
}

export type Action =
  | { type: 'refresh/start' }
  | { type: 'refresh/ok'; status: Status; cards: Card[]; jobs: Job[]; at: number }
  | { type: 'refresh/fail'; problem: Problem; at: number }
  | { type: 'card/answer'; id: string; decision: Decision }
  | { type: 'card/confirmed'; id: string; decision: Decision; always: boolean }
  | { type: 'card/failed'; id: string; reason: 'answered' | 'expired' | 'other' }
  | { type: 'pause/set'; paused: boolean }
  | { type: 'pause/confirmed'; paused: boolean }
  | { type: 'pause/failed' }
  | { type: 'notice/dismiss' }
  | { type: 'device/use-this' }
  | { type: 'tick'; at: number };

export function initialState(now: number, lastOkAt: number | null = null): State {
  return {
    loaded: false,
    refreshing: false,
    problem: null,
    status: null,
    cards: [],
    jobs: [],
    lastOkAt,
    now,
    answering: {},
    pausedOverride: null,
    notice: null,
    useThisDevice: false,
  };
}

export function problemOf(err: Pick<HomeError, 'kind' | 'code'>): Problem {
  if (err.kind === 'unreachable') return 'unreachable';
  if (err.code === 'unauthorized' || err.code === 'forbidden') return 'auth';
  return 'generic';
}

function notice(state: State, kind: NoticeKind): Notice {
  return { seq: (state.notice?.seq ?? 0) + 1, kind };
}

function without<T>(map: Record<string, T>, key: string): Record<string, T> {
  return Object.fromEntries(Object.entries(map).filter(([k]) => k !== key));
}

export function reducer(state: State, action: Action): State {
  switch (action.type) {
    case 'refresh/start':
      return { ...state, refreshing: true };
    case 'refresh/ok':
      return {
        ...state,
        loaded: true,
        refreshing: false,
        problem: null,
        status: action.status,
        cards: action.cards,
        jobs: action.jobs,
        lastOkAt: action.at,
        now: action.at,
        useThisDevice: false,
      };
    case 'refresh/fail':
      // The last known content stays on screen.
      return { ...state, refreshing: false, problem: action.problem, now: action.at };
    case 'card/answer':
      return { ...state, answering: { ...state.answering, [action.id]: action.decision }, notice: null };
    case 'card/confirmed':
      return {
        ...state,
        cards: state.cards.filter((c) => c.id !== action.id),
        answering: without(state.answering, action.id),
        notice: notice(state, action.decision === 'decline' ? 'declined' : action.always ? 'approved_always' : 'approved'),
      };
    case 'card/failed':
      if (action.reason === 'other') {
        // Roll back: the card is waiting again.
        return { ...state, answering: without(state.answering, action.id), notice: notice(state, 'card_failed') };
      }
      // Someone else answered it, or its time ran out: it is no longer waiting anywhere.
      return {
        ...state,
        cards: state.cards.filter((c) => c.id !== action.id),
        answering: without(state.answering, action.id),
        notice: notice(state, action.reason === 'answered' ? 'card_answered' : 'card_expired'),
      };
    case 'pause/set':
      return { ...state, pausedOverride: action.paused, notice: state.notice?.kind === 'pause_failed' ? null : state.notice };
    case 'pause/confirmed':
      return { ...state, pausedOverride: null, status: state.status ? { ...state.status, paused: action.paused } : state.status };
    case 'pause/failed':
      return { ...state, pausedOverride: null, notice: notice(state, 'pause_failed') };
    case 'notice/dismiss':
      return { ...state, notice: null };
    case 'device/use-this':
      return { ...state, useThisDevice: true };
    case 'tick':
      return { ...state, now: action.at };
  }
}

// ---- what the screens read

export function isPaused(state: State): boolean {
  return state.pausedOverride ?? state.status?.paused ?? false;
}

/** Cards that still need the person: waiting, not answered here a moment ago, not past their time. */
export function waitingCards(state: State): Card[] {
  return state.cards.filter((c) => c.status === 'pending' && !(c.id in state.answering) && Date.parse(c.expires_at) > state.now);
}
