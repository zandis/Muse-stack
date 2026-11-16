export const FAKE_TOKEN: string;
export const STATES: readonly string[];
export const REPO_ROOT: string;
export function freePort(): Promise<number>;
export function callHome(port: number, method: string, path: string, body?: unknown): Promise<{ status: number; body: any }>; // eslint-disable-line @typescript-eslint/no-explicit-any
export function startFakeHome(options?: { state?: string; port?: number; quiet?: boolean }): Promise<{ port: number; state: string; stop: () => Promise<void> }>;
