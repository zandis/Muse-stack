"""Permissions (the specification calls them mandates): one signed token per thing the
person allowed, narrowed offline for a skill, a tool call or a single approval.

This is the home's twin of `core/src/permissions.rs`. The format is
`docs/spec/interfaces/permission-token.md`; the token is a Biscuit, handled through
`biscuit-python` (optional extra `permissions`; it wraps the same Rust crate the core
uses, and this package does no cryptography of its own). Both implementations are held
to `core/conformance/permissions.json`: the same tokens, the same requests, the same
answers, reasons and sentences.

    propose_from_fields(Draft(...)) -> Draft         validate what the person will approve
    issue(draft, root_key_pair)     -> Mandate       sign it with the person's root key
    narrow(mandate, Narrowing(...)) -> Mandate       append restrictions; needs no key
    check(mandate, request, usage, revoked, spent, now) -> Decision
    describe(mandate, "en" | "zh-TW") -> Description the sentence and its lines
    revocation_ids(mandate), RevocationList, SpentSet

Everything except `muse.permissions.store` is a pure function of its arguments.
Nothing outside this package enforces these tokens yet (the compiler, work package C7,
wires them to the router and to skills).
"""

from muse.permissions.check import ALLOW, Decision, check, check_and_record, check_encoded, deny, single_use_keys, spent_key
from muse.permissions.describe import LANGS, Description, describe, format_amount, format_time
from muse.permissions.lists import RevocationEntry, RevocationList, SpentSet
from muse.permissions.model import (
    DEFAULT_SKEW,
    EACH_WRITE,
    MAX_SINGLE_USE_TTL,
    MAX_SKEW,
    MAX_TIME,
    NEVER,
    PRIVACY,
    SCHEMA_NAME,
    SCHEMA_VERSION,
    Amount,
    Cap,
    Confirm,
    Draft,
    Limit,
    NarrowLimit,
    Narrowing,
    PermissionError,
    Request,
    Resource,
    action_class,
    canonical_narrowing,
    propose_from_fields,
    valid_request,
)
from muse.permissions.token import (
    AUTHORIZER_LIMIT_CHECK,
    AUTHORIZER_POLICY,
    BISCUIT_PYTHON_VERSION,
    Mandate,
    available,
    engine_allows,
    generate_private_key,
    issue,
    mandate_id,
    narrow,
    parse_private_key,
    parse_public_key,
    public_key_text,
    revocation_ids,
    rfc3339,
    unverified_revocation_ids,
)

__all__ = [
    "ALLOW", "AUTHORIZER_LIMIT_CHECK", "AUTHORIZER_POLICY", "Amount", "BISCUIT_PYTHON_VERSION", "Cap", "Confirm", "DEFAULT_SKEW",
    "Decision", "Description", "Draft", "EACH_WRITE", "LANGS", "Limit", "MAX_SINGLE_USE_TTL", "MAX_SKEW", "MAX_TIME", "Mandate",
    "NEVER", "NarrowLimit", "Narrowing", "PRIVACY", "PermissionError", "Request", "Resource", "RevocationEntry", "RevocationList",
    "SCHEMA_NAME", "SCHEMA_VERSION", "SpentSet", "action_class", "available", "canonical_narrowing", "check", "check_and_record",
    "check_encoded", "deny", "describe", "engine_allows", "format_amount", "format_time", "generate_private_key", "issue",
    "mandate_id", "narrow", "parse_private_key", "parse_public_key", "propose_from_fields", "public_key_text", "revocation_ids",
    "rfc3339", "single_use_keys", "spent_key", "unverified_revocation_ids", "valid_request",
]
