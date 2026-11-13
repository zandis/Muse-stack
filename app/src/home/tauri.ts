// Inside the desktop app: the Rust side (src-tauri/src/home.rs) talks to the home's
// Unix socket and adds the device token it reads from a private file. The web view
// only ever sees `{ status, body }`.
import { HomeError, type Method, type Transport, type TransportReply } from './client';

export type Invoke = (cmd: string, args?: Record<string, unknown>) => Promise<unknown>;

export function isTauri(w: unknown = globalThis): boolean {
  return typeof w === 'object' && w !== null && '__TAURI_INTERNALS__' in w;
}

export async function tauriInvoke(cmd: string, args?: Record<string, unknown>): Promise<unknown> {
  const { invoke } = await import('@tauri-apps/api/core');
  return invoke(cmd, args);
}

export function tauriTransport(invoke: Invoke = tauriInvoke): Transport {
  return {
    async request(method: Method, path: string, body?: unknown): Promise<TransportReply> {
      if (!path.startsWith('/v1/')) throw new HomeError('protocol');
      let reply: unknown;
      try {
        reply = await invoke('home_request', { method, path, body: body === undefined ? null : body });
      } catch (err) {
        // The command rejects with a short code: `unreachable`, `no_token`, `invalid_request`, `bad_reply`.
        throw new HomeError(err === 'invalid_request' || err === 'bad_reply' ? 'protocol' : 'unreachable');
      }
      if (typeof reply !== 'object' || reply === null) throw new HomeError('protocol');
      const r = reply as { status?: unknown; body?: unknown };
      if (typeof r.status !== 'number') throw new HomeError('protocol');
      return { status: r.status, body: r.body ?? null };
    },
  };
}
