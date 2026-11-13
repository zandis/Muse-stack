// The only way the app opens anything outside itself. A link is opened only if it
// is https and its host is on this list. The Rust side (src-tauri/src/links.rs)
// holds the same list and checks again.
import { isTauri, tauriInvoke, type Invoke } from '../home/tauri';

export const ALLOWED_HOSTS: readonly string[] = ['chatgpt.com', 'claude.ai', 'gemini.google.com'];

export function allowedExternal(url: string, hosts: readonly string[] = ALLOWED_HOSTS): URL | null {
  let u: URL;
  try {
    u = new URL(url);
  } catch {
    return null;
  }
  if (u.protocol !== 'https:' || u.username || u.password || u.port) return null;
  return hosts.includes(u.hostname) ? u : null;
}

export interface Opener {
  tauri: boolean;
  invoke: Invoke;
  open: (url: string) => void;
}

function defaultOpener(): Opener {
  return {
    tauri: isTauri(),
    invoke: tauriInvoke,
    open: (url) => {
      window.open(url, '_blank', 'noopener,noreferrer');
    },
  };
}

/** Opens an allowed link in the person's browser. Returns false, and opens nothing, for anything else. */
export async function openExternal(url: string, opener: Opener = defaultOpener()): Promise<boolean> {
  const u = allowedExternal(url);
  if (!u) return false;
  if (opener.tauri) {
    try {
      await opener.invoke('open_external', { url: u.href });
    } catch {
      return false;
    }
    return true;
  }
  opener.open(u.href);
  return true;
}
