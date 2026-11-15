// Records and permissions, as the home sends them (docs/spec/interfaces/home-api.v1.yaml:
// /v1/receipts, /v1/receipts/export, /v1/mandates…; the record itself is
// docs/spec/interfaces/record-format.md section 1). Written by hand, like types.ts.
type Time = string;

export interface RecordCost {
  /** ISO 4217, or USDC. */
  currency: string;
  /** In the currency's minor unit (cents for USD and TWD; 6 decimals for USDC). */
  minor: number;
}

export type RecordActor = 'person' | 'helper' | 'system';
export type RecordOutcome = 'ok' | 'declined' | 'failed' | 'undone';

/** One signed record. `summary` and `summary_salt` are both missing when the record was redacted. */
export interface HomeRecord {
  v: number;
  device: string;
  seq: number;
  /** Milliseconds since 1970, the device's clock. */
  time: number;
  mandate: string | null;
  actor: RecordActor;
  action: string;
  resource: string | null;
  summary_hash: string;
  outcome: RecordOutcome;
  cost: RecordCost | null;
  links?: { job?: string; card?: string; payment_ref?: string; undo_of?: string };
  /** none | home | service:<id> | host:<name>; missing when the record does not say. */
  egress?: string;
  prev: string;
  sig: string;
  summary?: string;
  summary_salt?: string;
}

export interface RecordsVerify {
  ok: boolean;
  size: number;
  kind?: string;
  index?: number | null;
}

export interface RecordsSeal {
  size: number;
  root: string;
  time: Time;
}

export interface RecordsPage {
  records: HomeRecord[];
  order: 'newest_first';
  next_before: number | null;
  verify: RecordsVerify;
  sealed: RecordsSeal | null;
  device: string | null;
}

export interface RecordsExport {
  export: Record<string, unknown>;
  file_name: string;
  count: number;
  size: number;
  redacted: number;
  device: string;
  recorded: boolean;
}

export type TargetState = 'active' | 'pending' | 'failed' | 'not_built' | 'revoked';

export interface TargetStatus {
  state: TargetState;
  reason: string;
  note?: string;
}

export interface PermissionLimit {
  kind: string;
  unit: string;
  decimals: number;
  period: string;
  amount: number;
}

export interface Permission {
  id: string;
  name: string;
  holder: string;
  state: 'active' | 'stopped' | 'expired' | 'not_yet_valid';
  /** The home's own sentence and lines, in the language asked for. */
  sentence: string;
  lines: string[];
  actions: string[];
  resources: { kind: string; value: string }[];
  limits: PermissionLimit[];
  privacy: string;
  issued_at: Time;
  not_before: Time;
  not_after: Time;
  /** skills | router | records | payments | connectors | health -> how it stands there. */
  targets: Record<string, TargetStatus>;
}

export interface PermissionList {
  mandates: Permission[];
  enforcement: { skills: 'off' | 'advise' | 'enforce'; skills_not_shipped_with_muse: string; router: string; not_built: string[] };
}

export interface StopResult {
  stopped: true;
  was: string;
  id: string;
  name: string;
  targets: Record<string, TargetStatus>;
  complete: boolean;
  recorded: boolean;
}

export interface DeclinedRequest {
  card_id: string;
  device: string;
  holder: string;
  actions: string[];
  resources: { kind: string; value: string }[];
  sentence: string | null;
  declined_at: Time;
  ask_again_after: Time;
}
