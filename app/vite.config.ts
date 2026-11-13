import type { Plugin } from 'vite';
import { defineConfig } from 'vitest/config';
import { CONTENT_SECURITY_POLICY, homeProxy } from './home-proxy.ts';

// Inside the Tauri window the policy comes from src-tauri/tauri.conf.json (it also
// has to allow Tauri's own message channel). In a browser it is this meta tag plus
// the same value as a response header from `vite preview`.
const forTauri = Boolean(process.env.TAURI_ENV_PLATFORM);

function contentSecurityPolicy(): Plugin {
  return {
    name: 'muse-csp',
    apply: 'build',
    transformIndexHtml: () =>
      forTauri ? [] : [{ tag: 'meta', attrs: { 'http-equiv': 'Content-Security-Policy', content: CONTENT_SECURITY_POLICY }, injectTo: 'head-prepend' }],
  };
}

const proxy = homeProxy(process.env);
const securityHeaders = {
  'X-Content-Type-Options': 'nosniff',
  'Referrer-Policy': 'no-referrer',
  'X-Frame-Options': 'DENY',
};

export default defineConfig({
  plugins: [contentSecurityPolicy()],
  oxc: { jsx: { runtime: 'automatic' } },
  clearScreen: false,
  build: {
    target: 'es2022',
    sourcemap: false,
    assetsInlineLimit: 0, // nothing inlined as data: so the policy can stay strict
  },
  server: { host: '127.0.0.1', port: 5173, strictPort: true, proxy, headers: securityHeaders },
  preview: {
    host: '127.0.0.1',
    port: 4173,
    strictPort: true,
    proxy,
    headers: { ...securityHeaders, 'Content-Security-Policy': `${CONTENT_SECURITY_POLICY}; frame-ancestors 'none'` },
  },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.{ts,tsx}'],
    restoreMocks: true,
  },
});
