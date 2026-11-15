// The only way the app opens anything outside itself. A link is opened only if it
// is https and its host is on this list. The list is the catalogue's own sign-up,
// key and sign-in pages and the three subscription sites, fixed when the app is
// built (link-hosts.json, written by scripts/export_link_hosts.py). The desktop
// shell compiles the same file into Rust (src-tauri/crates/home-link) and checks
// every link again there, so nothing the web view does can add a host.
import { isTauri, tauriInvoke, type Invoke } from '../home/tauri';
import linkHosts from './link-hosts.json';

export const ALLOWED_HOSTS: readonly string[] = Object.freeze([...linkHosts.hosts]);

/**
 * Hosts of the services' own key pages, as the home's catalogue names them (GET /v1/services `key_host`).
 * Added while the app runs, never removed; a plain DNS name only, so nothing else can be smuggled in.
 * In the browser build this covers a home whose catalogue is newer than the app. The desktop shell
 * ignores it: Rust opens only the hosts compiled into it.
 */
const catalogueHosts = new Set<string>();
const DNS_NAME = /^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$/;

export function allowCatalogueHosts(hosts: readonly (string | null | undefined)[]): void {
  for (const h of hosts) if (typeof h === 'string' && DNS_NAME.test(h)) catalogueHosts.add(h);
}

export function allowedHosts(): string[] {
  return [...ALLOWED_HOSTS, ...catalogueHosts];
}

export function allowedExternal(url: string, hosts: readonly string[] = allowedHosts()): URL | null {
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
