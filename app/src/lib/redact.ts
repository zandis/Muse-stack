// Defence in depth. The home promises that no reply holds a key or token. If one
// ever slips through (a bug on the home, a hostile card), the app still does not
// put it on the screen: every piece of text that comes from the API passes here.

export const HIDDEN = '[…]';

const PATTERNS: readonly RegExp[] = [
  // Prefixes services use for their keys: sk-…, gsk_…, ghp_…, hf_…, xoxb-…, AIza…
  /\b(?:sk|pk|rk|gsk|csk|ghp|gho|ghu|ghs|glpat|hf|nvapi|xai|r8|or|pplx)[-_][A-Za-z0-9_-]{12,}/g,
  /\bxox[abpors]-[A-Za-z0-9-]{10,}/g,
  /\bAIza[0-9A-Za-z_-]{20,}/g,
  /\bmuse_(?:dev|app)_[A-Za-z0-9_-]{16,}/g,
  // "Bearer <something long>"
  /\bBearer\s+[A-Za-z0-9._~+/-]{16,}=*/gi,
  // JSON web tokens
  /\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{4,}/g,
];

// Any other unbroken run of 40 or more key-like characters that mixes letters and digits.
// No slash or dot in the set, so ordinary file paths and names are left alone.
const LONG_RUN = /[A-Za-z0-9_-]{40,}/g;

function looksRandom(run: string): boolean {
  return /[0-9]/.test(run) && /[A-Za-z]/.test(run);
}

export function redact(text: string): string {
  let out = text;
  for (const re of PATTERNS) out = out.replace(re, HIDDEN);
  return out.replace(LONG_RUN, (run) => (looksRandom(run) ? HIDDEN : run));
}

/** A file fingerprint is the one long value that may be shown, and only when it is exactly 64 hex digits. */
export function fingerprint(value: unknown): string | null {
  return typeof value === 'string' && /^[0-9a-f]{64}$/.test(value) ? value : null;
}
