// Small things remembered on this device only: the language choice, when the home
// was last seen, and which tip was shown today. Nothing from the home's replies
// other than those is stored. Storage can be unavailable (private windows); then
// the app simply forgets.
export function readStored(key: string): string | null {
  try {
    return globalThis.localStorage?.getItem(key) ?? null;
  } catch {
    return null;
  }
}

export function writeStored(key: string, value: string | null): void {
  try {
    if (value === null) globalThis.localStorage?.removeItem(key);
    else globalThis.localStorage?.setItem(key, value);
  } catch {
    /* forgotten */
  }
}
