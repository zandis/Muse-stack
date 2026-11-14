// The app-screens client, given to every screen through context, and two small
// helpers the screens share: loading a value from the home with the section 8
// states, and remembering a sentence handed from Ask to Chores.
import { createContext, useCallback, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { AppClient } from '../home/app-client';
import { HomeError } from '../home/client';
import { problemOf, type Problem } from './reducer';

const Ctx = createContext<AppClient | null>(null);

export function AppClientProvider({ client, children }: { client: AppClient; children: ReactNode }) {
  return <Ctx.Provider value={client}>{children}</Ctx.Provider>;
}

export function useAppClient(): AppClient {
  const v = useContext(Ctx);
  if (!v) throw new Error('useAppClient needs an AppClientProvider');
  return v;
}

export function problemFrom(err: unknown): Problem {
  return problemOf(err instanceof HomeError ? err : new HomeError('protocol'));
}

export interface Loaded<T> {
  data: T | null;
  /** True while a request is out. The last known data stays shown (section 8: never a blank screen). */
  loading: boolean;
  problem: Problem | null;
  reload: () => Promise<void>;
  set: (data: T) => void;
}

/** Load once on mount, then on `reload`. A reply that arrives after a newer one started is dropped. */
export function useLoad<T>(load: () => Promise<T>): Loaded<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState(true);
  const [problem, setProblem] = useState<Problem | null>(null);
  const seq = useRef(0);
  const loadRef = useRef(load);
  useEffect(() => {
    loadRef.current = load;
  }, [load]);
  const reload = useCallback(async () => {
    const mine = ++seq.current;
    setLoading(true);
    try {
      const v = await loadRef.current();
      if (mine !== seq.current) return;
      setData(v);
      setProblem(null);
    } catch (err) {
      if (mine !== seq.current) return;
      setProblem(problemFrom(err));
    } finally {
      if (mine === seq.current) setLoading(false);
    }
  }, []);
  useEffect(() => {
    void reload();
  }, [reload]);
  return { data, loading, problem, reload, set: setData };
}

// "Make this a chore": the question goes from Ask to the Chores screen's sentence box. Memory only.
let choreDraft: string | null = null;

export function setChoreDraft(sentence: string): void {
  choreDraft = sentence;
}

export function takeChoreDraft(): string | null {
  const v = choreDraft;
  choreDraft = null;
  return v;
}
