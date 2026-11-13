// Polling, not websockets. One request round at a time; slower after failures;
// nothing at all while the window is hidden; at once when it is shown again.

export const BASE_INTERVAL_MS = 5_000;
export const MAX_INTERVAL_MS = 60_000;

/** How long to wait before the next round. `retryAfterS` is what the home asked for, if it did. */
export function nextDelay(failures: number, retryAfterS: number | null = null, base = BASE_INTERVAL_MS, max = MAX_INTERVAL_MS): number {
  const backoff = Math.min(max, base * 2 ** Math.min(Math.max(0, failures), 10));
  const asked = retryAfterS !== null && retryAfterS > 0 ? Math.min(max, retryAfterS * 1000) : 0;
  return Math.max(backoff, asked);
}

export interface RoundResult {
  ok: boolean;
  retryAfterS?: number | null;
}

export interface PollerOptions {
  run: () => Promise<RoundResult>;
  isVisible: () => boolean;
  setTimer?: (fn: () => void, ms: number) => unknown;
  clearTimer?: (id: unknown) => void;
  base?: number;
  max?: number;
}

export class Poller {
  private failures = 0;
  private timer: unknown = null;
  private running = false;
  private stopped = true;
  private again = false;
  private readonly setTimer: (fn: () => void, ms: number) => unknown;
  private readonly clearTimer: (id: unknown) => void;

  constructor(private readonly o: PollerOptions) {
    this.setTimer = o.setTimer ?? ((fn, ms) => setTimeout(fn, ms));
    this.clearTimer = o.clearTimer ?? ((id) => clearTimeout(id as ReturnType<typeof setTimeout>));
  }

  start(): void {
    this.stopped = false;
    void this.round();
  }

  stop(): void {
    this.stopped = true;
    this.cancelTimer();
  }

  /** Ask now (Retry button, after a change, when the window is shown again). */
  kick(): void {
    if (this.stopped) return;
    if (this.running) {
      this.again = true;
      return;
    }
    this.cancelTimer();
    void this.round();
  }

  /** Call when the window is hidden or shown. */
  visibilityChanged(): void {
    if (this.stopped) return;
    if (this.o.isVisible()) this.kick();
    else this.cancelTimer();
  }

  private cancelTimer(): void {
    if (this.timer !== null) this.clearTimer(this.timer);
    this.timer = null;
  }

  private async round(): Promise<void> {
    if (this.stopped || this.running) return;
    if (!this.o.isVisible()) return; // resumes from visibilityChanged()
    this.running = true;
    let result: RoundResult;
    try {
      result = await this.o.run();
    } catch {
      result = { ok: false };
    }
    this.running = false;
    if (this.stopped) return;
    this.failures = result.ok ? 0 : this.failures + 1;
    if (this.again) {
      this.again = false;
      void this.round();
      return;
    }
    if (!this.o.isVisible()) return;
    const delay = nextDelay(this.failures, result.retryAfterS ?? null, this.o.base, this.o.max);
    this.timer = this.setTimer(() => {
      this.timer = null;
      void this.round();
    }, delay);
  }
}
