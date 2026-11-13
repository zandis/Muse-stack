// The allowance gauge: a 270-degree arc. The arc is decoration (aria-hidden); the
// number beside or inside it is real text, and the state is never carried by
// colour alone (an estimate is drawn in dashes and labelled "about").
import type { ReactNode } from 'react';

const ARC = 'M43.43 156.57A80 80 0 1 1 156.57 156.57';

/** The dash pattern for an estimated value: short dashes up to the value. */
export function estimateDashes(percent: number): string {
  let s = '';
  let n = 0;
  while (n + 3.4 < percent) {
    s += '2.2 1.2 ';
    n += 3.4;
  }
  return `${s}0 100`;
}

export interface GaugeProps {
  /** 0..100 */
  percent: number;
  /** Width in CSS pixels. */
  size: number;
  estimated?: boolean;
  paused?: boolean;
  mini?: boolean;
  children?: ReactNode;
}

export function Gauge({ percent, size, estimated = false, paused = false, mini = false, children }: GaugeProps) {
  const pct = Math.min(100, Math.max(0, percent));
  const level = pct <= 0 ? ' none' : pct < 25 ? ' low' : '';
  const stroke = mini ? 18 : 16;
  return (
    <span className={`gauge${level}${paused ? ' paused' : ''}`}>
      <svg viewBox="0 0 200 172" width={size} height={Math.round((size * 172) / 200)} aria-hidden="true" focusable="false">
        <path className="track" d={ARC} pathLength={100} fill="none" strokeWidth={stroke} strokeLinecap="round" />
        {pct > 0 ? (
          <path
            className="fill"
            d={ARC}
            pathLength={100}
            fill="none"
            strokeWidth={stroke}
            strokeLinecap={estimated ? 'butt' : 'round'}
            strokeDasharray={estimated ? estimateDashes(pct) : `${pct} 100`}
          />
        ) : null}
      </svg>
      {children ? <span className="center">{children}</span> : null}
    </span>
  );
}
