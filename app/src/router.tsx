// A very small router over the History API: eleven screens and the tray panel,
// one path segment each. Small enough that a routing library would be more code
// to audit than this file.
import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type MouseEvent, type ReactNode } from 'react';

export const SCREENS = ['today', 'ask', 'chores', 'records', 'more', 'services', 'permissions', 'devices', 'wallet', 'apps', 'settings'] as const;
export type Screen = (typeof SCREENS)[number];
export type Route = Screen | 'tray';

/** The four main tabs, then what "More" holds (docs/spec/02-ux.md section 2). */
export const NAV_MAIN: readonly Screen[] = ['today', 'ask', 'chores', 'records'];
export const NAV_MORE: readonly Screen[] = ['services', 'permissions', 'devices', 'wallet', 'apps', 'settings'];

export function parseRoute(pathname: string): Route {
  const seg = pathname.replace(/^\/+|\/+$/g, '').split('/')[0] ?? '';
  if (seg === 'tray') return 'tray';
  return (SCREENS as readonly string[]).includes(seg) ? (seg as Screen) : 'today';
}

export function pathOf(route: Route): string {
  return `/${route}`;
}

interface Router {
  route: Route;
  /** Counts navigations made inside the app, so the layout can move focus to the new screen's heading. */
  moves: number;
  /** `replace` swaps the current history entry (used for redirects) and does not move focus. */
  go: (route: Route, replace?: boolean) => void;
}

const Ctx = createContext<Router | null>(null);

export function RouterProvider({ children }: { children: ReactNode }) {
  const [route, setRoute] = useState<Route>(() => parseRoute(window.location.pathname));
  const [moves, setMoves] = useState(0);
  const shown = useRef(route);

  useEffect(() => {
    const onPop = () => {
      const next = parseRoute(window.location.pathname);
      // A jump within the page (the skip link) also fires popstate; only a change of screen moves focus.
      if (next !== shown.current) setMoves((n) => n + 1);
      shown.current = next;
      setRoute(next);
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, []);

  const go = useCallback((next: Route, replace = false) => {
    if (window.location.pathname !== pathOf(next)) {
      if (replace) window.history.replaceState(null, '', pathOf(next));
      else window.history.pushState(null, '', pathOf(next));
    }
    shown.current = next;
    setRoute(next);
    if (!replace) setMoves((n) => n + 1);
  }, []);

  const value = useMemo(() => ({ route, moves, go }), [route, moves, go]);
  return <Ctx.Provider value={value}>{children}</Ctx.Provider>;
}

export function useRouter(): Router {
  const v = useContext(Ctx);
  if (!v) throw new Error('useRouter needs a RouterProvider');
  return v;
}

/** A real link (it has an address, opens in a new tab, works with a screen reader) that navigates in place. */
export function Link({ to, className, children, current, label }: { to: Route; className?: string; children: ReactNode; current?: boolean; label?: string }) {
  const { go } = useRouter();
  const onClick = (e: MouseEvent<HTMLAnchorElement>) => {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    go(to);
  };
  return (
    <a href={pathOf(to)} className={className} onClick={onClick} aria-current={current ? 'page' : undefined} aria-label={label}>
      {children}
    </a>
  );
}
