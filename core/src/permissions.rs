//! Permissions (the specification calls them mandates): one signed token per thing the
//! person allowed, narrowed offline for a skill, a tool call or a single approval.
//!
//! The format is `docs/spec/interfaces/permission-token.md`; the token is a Biscuit
//! (`biscuit-auth`, pinned exactly). The Python twin is `muse/permissions/`, and both
//! are held to `core/conformance/permissions.json`: the same tokens, the same requests,
//! the same answers, reasons and sentences.
//!
//! Everything here is a pure function of its arguments. There is no storage and no
//! clock: `now` is passed in, usage figures come from the caller's ledger, and the
//! revocation list and spent set are values the caller loads and saves.
//!
//! - [`propose_from_fields`]: validate the fields of a permission into a [`Draft`].
//! - [`issue`]: sign a draft with the person's root key -> [`Mandate`].
//! - [`narrow`]: append a block of restrictions; needs no key.
//! - [`check`]: allow, deny with a reason, or ask for confirmation.
//! - [`describe`]: the sentence and its lines, in English or Traditional Chinese.
//! - [`revocation_ids`], [`RevocationList`], [`SpentSet`].
//!
//! Nothing outside this module enforces these tokens yet.

mod check;
mod describe;
mod lists;
mod model;
mod token;

pub use check::{check, check_and_record, check_encoded, check_with_skew, single_use_keys, spent_key, Decision, Usage};
pub use describe::{describe, format_amount, format_time, Description, Lang};
pub use lists::{RevocationEntry, RevocationList, SpentSet};
pub use model::{
    action_class, canonical_narrowing, propose_from_fields, valid_request, Amount, Cap, Confirm, Draft, Limit, NarrowLimit, Narrowing, PermissionError,
    Privacy, Request, Resource, DEFAULT_SKEW, MAX_SINGLE_USE_TTL, MAX_SKEW, MAX_TIME, SCHEMA_NAME, SCHEMA_VERSION,
};
pub use token::{
    engine_allows, issue, mandate_id, narrow, parse_private_key, parse_public_key, public_key_text, revocation_ids, rfc3339, unverified_revocation_ids,
    Mandate, AUTHORIZER_LIMIT_CHECK, AUTHORIZER_POLICY,
};

/// The root key pair type (from the Biscuit library; this crate does no cryptography of its own).
pub use biscuit_auth::KeyPair;
