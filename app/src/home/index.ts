import { createClient, type HomeClient } from './client';
import { httpTransport } from './http';
import { isTauri, tauriTransport } from './tauri';

export * from './client';
export * from './types';

/** The client for where the app is running: the Tauri command inside the desktop app, HTTP otherwise. */
export function defaultClient(): HomeClient {
  return createClient(isTauri() ? tauriTransport() : httpTransport());
}
