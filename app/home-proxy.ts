// The development and test bridge to a home that listens on 127.0.0.1.
//
// The home sends no CORS headers, so a page cannot call it directly. `vite dev`
// and `vite preview` forward same-origin `/v1/...` requests to it and add the
// device token here, on the server side. The token comes from the environment
// and is never part of the bundle the browser downloads.
import type { IncomingMessage } from 'node:http';
import type { ProxyOptions } from 'vite';

export const CONTENT_SECURITY_POLICY = [
  "default-src 'none'",
  "script-src 'self'",
  "style-src 'self'",
  "img-src 'self'",
  "font-src 'self'",
  "connect-src 'self'",
  "manifest-src 'self'",
  "base-uri 'none'",
  "form-action 'none'",
  "object-src 'none'",
].join('; ');

/** True when the request comes from this app's own pages (or from a tool that is not a browser). */
export function sameOrigin(req: Pick<IncomingMessage, 'headers'>): boolean {
  const site = req.headers['sec-fetch-site'];
  if (typeof site === 'string' && site !== 'same-origin' && site !== 'none') return false;
  const origin = req.headers.origin;
  if (typeof origin === 'string') {
    try {
      if (new URL(origin).host !== req.headers.host) return false;
    } catch {
      return false;
    }
  }
  return true;
}

export function homeProxy(env: Record<string, string | undefined>): Record<string, ProxyOptions> {
  const port = Number(env.MUSE_HOME_PORT ?? 4181);
  const token = env.MUSE_HOME_TOKEN ?? '';
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw new Error('MUSE_HOME_PORT must be a port number');
  return {
    '/v1': {
      target: `http://127.0.0.1:${port}`,
      changeOrigin: true, // the home checks that Host is 127.0.0.1:<port>
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      // Another site open in the same browser must not be able to use the token through this proxy.
      bypass: (req) => (sameOrigin(req) ? undefined : false),
      configure: (proxy) => {
        proxy.on('proxyReq', (proxyReq) => {
          proxyReq.removeHeader('origin');
          proxyReq.removeHeader('cookie');
          proxyReq.removeHeader('referer');
        });
      },
    },
  };
}
