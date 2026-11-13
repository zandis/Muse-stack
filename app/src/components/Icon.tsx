// Line icons, drawn inline (no icon font, no network). The shapes are the mock-up's
// (docs/mockups/index.html), rewritten as elements so nothing is set through innerHTML.
import type { ReactNode } from 'react';

const p = (d: string, key = d) => <path key={key} d={d} />;
const c = (cx: number, cy: number, r: number) => <circle key={`${cx}-${cy}-${r}`} cx={cx} cy={cy} r={r} />;

const ICONS = {
  today: [p('M4 17a8 8 0 1 1 16 0'), p('M12 17l4-5'), c(12, 17, 1)],
  ask: [p('M21 12a8 8 0 0 1-11.6 7.1L4 20l1-4.6A8 8 0 1 1 21 12z')],
  chores: [p('M9 6h11M9 12h11M9 18h11'), p('M3.5 6l1.2 1.2L7 5M3.5 12l1.2 1.2L7 11M3.5 18l1.2 1.2L7 17')],
  records: [p('M6 3h12v18l-3-2-3 2-3-2-3 2z'), p('M9 8h6M9 12h6')],
  more: [c(5, 12, 1.6), c(12, 12, 1.6), c(19, 12, 1.6)],
  services: [p('M9 2v6M15 2v6'), p('M6 8h12v3a6 6 0 0 1-12 0V8z'), p('M12 17v5')],
  permissions: [p('M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6z'), p('M9 12l2 2 4-4')],
  devices: [p('M7 2h10a1 1 0 0 1 1 1v18a1 1 0 0 1-1 1H7a1 1 0 0 1-1-1V3a1 1 0 0 1 1-1z'), p('M11 18h2')],
  wallet: [p('M3 7a2 2 0 0 1 2-2h13v4M3 7v11a2 2 0 0 0 2 2h14a1 1 0 0 0 1-1v-9a1 1 0 0 0-1-1H5a2 2 0 0 1-2-2M16 14h2')],
  apps: [p('M10 14a5 5 0 0 0 7 0l3-3a5 5 0 0 0-7-7l-1 1M14 10a5 5 0 0 0-7 0l-3 3a5 5 0 0 0 7 7l1-1')],
  settings: [p('M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12M20 18h0'), c(16, 6, 2), c(10, 12, 2), c(18, 18, 2)],
  pause: [p('M8 5v14M16 5v14')],
  play: [p('M7 4l12 8-12 8z')],
  check: [p('M5 12l5 5L20 7')],
  x: [p('M6 6l12 12M18 6L6 18')],
  right: [p('M9 6l6 6-6 6')],
  left: [p('M15 6l-6 6 6 6')],
  external: [p('M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5')],
  hourglass: [p('M6 2h12M6 22h12M7 2v4l5 6-5 6v4M17 2v4l-5 6 5 6v4')],
  alert: [p('M12 3l10 18H2z'), p('M12 10v5M12 18h.01')],
  info: [c(12, 12, 9), p('M12 11v5M12 8h.01')],
  lock: [p('M6 11h12v10H6zM8 11V7a4 4 0 0 1 8 0v4')],
  bolt: [p('M13 2L4 14h7l-1 8 9-12h-7z')],
  cloud: [p('M7 18a4 4 0 0 1-.5-8A6 6 0 0 1 18 9.5a4.2 4.2 0 0 1-1 8.5z'), p('M4 4l16 16')],
  empty: [<rect key="r" x="4" y="5" width="16" height="14" rx="3" />, p('M8 10h8M8 14h5')],
} satisfies Record<string, ReactNode[]>;

export type IconName = keyof typeof ICONS;

export function Icon({ name, className }: { name: IconName; className?: string }) {
  return (
    <svg viewBox="0 0 24 24" className={className ? `i ${className}` : 'i'} aria-hidden="true" focusable="false">
      {ICONS[name]}
    </svg>
  );
}
