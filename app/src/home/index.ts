import { createAppClient, type AppClient } from './app-client';
import { createClient, type HomeClient, type Transport } from './client';
import { httpTransport } from './http';
import { isTauri, tauriTransport } from './tauri';

export * from './client';
export * from './types';
export * from './app-client';

function transport(): Transport {
  return isTauri() ? tauriTransport() : httpTransport();
}

/** The client for where the app is running: the Tauri command inside the desktop app, HTTP otherwise. */
export function defaultClient(): HomeClient {
  return createClient(transport());
}

/** The client for the app's screens (Ask, Chores, Services, Settings, For apps), over the same transport. */
export function defaultAppClient(): AppClient {
  return createAppClient(transport());
}
