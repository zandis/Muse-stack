// The store: React context plus a reducer (reducer.ts). This file owns the effects:
// polling the home, answering cards and the pause switch with optimistic updates,
// and the two small things remembered on the device.
import { createContext, useCallback, useContext, useEffect, useMemo, useReducer, useRef, type ReactNode } from 'react';
import { HomeError, type HomeClient } from '../home/client';
import type { Decision } from '../home/types';
import { readStored, writeStored } from '../lib/storage';
import { Poller, type RoundResult } from './poller';
import { initialState, problemOf, reducer, type State } from './reducer';

export const LAST_SEEN_KEY = 'muse.lastSeen';

export interface Actions {
  /** Ask the home again now. */
  retry: () => void;
  answerCard: (id: string, decision: Decision, always?: boolean) => Promise<void>;
  setPaused: (paused: boolean) => Promise<void>;
  dismissNotice: () => void;
  useThisDevice: () => void;
}

const StateCtx = createContext<State | null>(null);
const ActionsCtx = createContext<Actions | null>(null);

function storedLastSeen(): number | null {
  const n = Number(readStored(LAST_SEEN_KEY));
  return Number.isFinite(n) && n > 0 ? n : null;
}

export interface StoreProps {
  client: HomeClient;
  children: ReactNode;
  /** Tests pass a clock. */
  now?: () => number;
  /** Tests switch polling off and drive `retry` themselves. */
  poll?: boolean;
}

export function StoreProvider({ client, children, now = Date.now, poll = true }: StoreProps) {
  const [state, dispatch] = useReducer(reducer, undefined, () => initialState(now(), storedLastSeen()));
  const poller = useRef<Poller | null>(null);
  // Counts changes made from this device. A round that started before a change
  // finished is thrown away, so stale data never undoes what the person just did.
  const changes = useRef(0);

  const round = useCallback(async (): Promise<RoundResult> => {
    const startedAt = changes.current;
    dispatch({ type: 'refresh/start' });
    try {
      const [status, cards, jobs] = await Promise.all([client.status(), client.cards('pending'), client.jobs()]);
      const at = now();
      if (changes.current !== startedAt) {
        poller.current?.kick();
        return { ok: true };
      }
      dispatch({ type: 'refresh/ok', status, cards: cards.cards, jobs, at });
      writeStored(LAST_SEEN_KEY, String(at));
      return { ok: true };
    } catch (err) {
      const e = err instanceof HomeError ? err : new HomeError('protocol');
      dispatch({ type: 'refresh/fail', problem: problemOf(e), at: now() });
      return { ok: false, retryAfterS: e.retryAfter };
    }
  }, [client, now]);

  useEffect(() => {
    if (!poll) {
      void round();
      return;
    }
    const p = new Poller({ run: round, isVisible: () => document.visibilityState !== 'hidden' });
    poller.current = p;
    const onVisibility = () => p.visibilityChanged();
    document.addEventListener('visibilitychange', onVisibility);
    p.start();
    return () => {
      document.removeEventListener('visibilitychange', onVisibility);
      p.stop();
      poller.current = null;
    };
  }, [poll, round]);

  const actions = useMemo<Actions>(() => {
    const refresh = () => {
      if (poller.current) poller.current.kick();
      else void round();
    };
    return {
      retry: refresh,
      async answerCard(id, decision, always = false) {
        dispatch({ type: 'card/answer', id, decision }); // optimistic: the card leaves the list at once
        try {
          await client.answerCard(id, decision, always);
          changes.current += 1;
          dispatch({ type: 'card/confirmed', id, decision, always: decision === 'approve' && always });
        } catch (err) {
          changes.current += 1;
          const code = err instanceof HomeError ? err.code : null;
          dispatch({ type: 'card/failed', id, reason: code === 'card_already_answered' ? 'answered' : code === 'card_expired' ? 'expired' : 'other' });
        }
        refresh();
      },
      async setPaused(paused) {
        dispatch({ type: 'pause/set', paused }); // optimistic: the bar shows at once
        try {
          if (paused) await client.pause();
          else await client.resume();
          changes.current += 1;
          dispatch({ type: 'pause/confirmed', paused });
        } catch {
          changes.current += 1;
          dispatch({ type: 'pause/failed' });
        }
        refresh();
      },
      dismissNotice: () => dispatch({ type: 'notice/dismiss' }),
      useThisDevice: () => dispatch({ type: 'device/use-this' }),
    };
  }, [client, round]);

  return (
    <StateCtx.Provider value={state}>
      <ActionsCtx.Provider value={actions}>{children}</ActionsCtx.Provider>
    </StateCtx.Provider>
  );
}

export function useAppState(): State {
  const v = useContext(StateCtx);
  if (!v) throw new Error('useAppState needs a StoreProvider');
  return v;
}

export function useActions(): Actions {
  const v = useContext(ActionsCtx);
  if (!v) throw new Error('useActions needs a StoreProvider');
  return v;
}
