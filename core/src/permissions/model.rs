//! The values a permission is made of, and the rules for each of them.
//! `docs/spec/interfaces/permission-token.md` section 3 is the specification.

use std::collections::BTreeSet;

pub const SCHEMA_NAME: &str = "muse.permission";
pub const SCHEMA_VERSION: i64 = 1;
/// 9999-12-31T23:59:59Z: the last second a token may name.
pub const MAX_TIME: i64 = 253_402_300_799;
pub const DEFAULT_SKEW: i64 = 60;
pub const MAX_SKEW: i64 = 300;
/// A token bound to one tool call or one card must end within this many seconds.
pub const MAX_SINGLE_USE_TTL: i64 = 86_400;

pub const RESOURCE_KINDS: [&str; 6] = ["folder", "host", "calendar", "record_kind", "provider", "home"];
pub const HOLDER_KINDS: [&str; 4] = ["device", "helper", "app", "clinician"];
pub const READ_ACTIONS: [&str; 6] = ["files.read", "mail.read", "calendar.read", "health.read", "records.read", "llm.call"];
pub const SPEND_ACTIONS: [&str; 2] = ["pay.spend", "llm.paid"];

/// A failure with a machine-readable code (the same strings as the Python twin).
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct PermissionError {
    pub code: &'static str,
    pub detail: String,
}

impl PermissionError {
    pub fn new(code: &'static str, detail: impl Into<String>) -> Self {
        PermissionError { code, detail: detail.into() }
    }
}

impl std::fmt::Display for PermissionError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.detail)
    }
}

impl std::error::Error for PermissionError {}

pub(crate) fn fail<T>(code: &'static str, detail: impl Into<String>) -> Result<T, PermissionError> {
    Err(PermissionError::new(code, detail))
}

/// The class a request is handled under. Order matters: `Local` is the strictest.
#[derive(Debug, Clone, Copy, PartialEq, Eq, PartialOrd, Ord)]
pub enum Privacy {
    Local,
    Private,
    Standard,
    Public,
}

impl Privacy {
    pub fn rank(self) -> i64 {
        self as i64
    }

    pub fn name(self) -> &'static str {
        ["local", "private", "standard", "public"][self as usize]
    }

    pub fn from_rank(rank: i64) -> Option<Privacy> {
        [Privacy::Local, Privacy::Private, Privacy::Standard, Privacy::Public].get(usize::try_from(rank).ok()?).copied()
    }

    pub fn from_name(name: &str) -> Option<Privacy> {
        (0..4).filter_map(Privacy::from_rank).find(|p| p.name() == name)
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Resource {
    pub kind: String,
    pub value: String,
}

impl Resource {
    pub fn new(kind: &str, value: &str) -> Resource {
        Resource { kind: kind.to_string(), value: value.to_string() }
    }

    /// True if a permission for `self` includes `other` (segment-aware for folders).
    pub fn covers(&self, other: &Resource) -> bool {
        if self.kind != other.kind {
            return false;
        }
        if self.value == "*" || self.value == other.value {
            return true;
        }
        if other.value == "*" {
            return false;
        }
        self.kind == "folder" && other.value.strip_prefix(self.value.as_str()).is_some_and(|rest| rest.starts_with('/'))
    }

    /// The resources both allow, if any.
    pub fn intersect(&self, other: &Resource) -> Option<Resource> {
        if self.covers(other) {
            Some(other.clone())
        } else if other.covers(self) {
            Some(self.clone())
        } else {
            None
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Limit {
    pub kind: String,
    pub unit: String,
    pub decimals: i64,
    pub period: String,
    pub amount: i64,
}

/// A lower limit added by narrowing (decimals come from the authority block).
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct NarrowLimit {
    pub kind: String,
    pub unit: String,
    pub period: String,
    pub amount: i64,
}

/// The most a single request may carry.
#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Cap {
    pub kind: String,
    pub unit: String,
    pub amount: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Confirm {
    EachWrite,
    OverAmount { unit: String, decimals: i64, amount: i64 },
    Never,
}

impl Confirm {
    pub fn name(&self) -> &'static str {
        match self {
            Confirm::EachWrite => "each_write",
            Confirm::OverAmount { .. } => "over_amount",
            Confirm::Never => "never",
        }
    }
}

/// The structured form a person approves. `propose_from_fields` returns it validated and sorted.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Draft {
    pub issuer: String,
    pub holder: String,
    pub actions: Vec<String>,
    pub routine: Vec<String>,
    pub resources: Vec<Resource>,
    pub limits: Vec<Limit>,
    pub purpose_text: String,
    pub purpose_code: String,
    pub issued_at: i64,
    pub not_before: i64,
    pub not_after: i64,
    pub confirm: Confirm,
    pub privacy: Privacy,
    pub period_zone: String,
}

/// What one appended block restricts. Empty fields restrict nothing.
#[derive(Debug, Clone, PartialEq, Eq, Default)]
pub struct Narrowing {
    pub actions: Vec<String>,
    pub resources: Vec<Resource>,
    pub limits: Vec<NarrowLimit>,
    pub caps: Vec<Cap>,
    pub not_before: Option<i64>,
    pub not_after: Option<i64>,
    pub privacy: Option<Privacy>,
    pub confirm: Option<Confirm>,
    pub job: Option<String>,
    pub tool: Option<String>,
    pub card: Option<String>,
}

#[derive(Debug, Clone, PartialEq, Eq, PartialOrd, Ord)]
pub struct Amount {
    pub kind: String,
    pub unit: String,
    pub value: i64,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    pub holder: String,
    pub action: String,
    pub resource: Resource,
    pub amounts: Vec<Amount>,
    pub privacy: String,
    pub job: Option<String>,
    pub tool: Option<String>,
    pub card: Option<String>,
}

// ---------------------------------------------------------------- text rules

/// No double quote, backslash or control character; at most `max_chars` characters.
pub fn is_safe_text(s: &str, max_chars: usize) -> bool {
    s.chars().count() <= max_chars && !s.chars().any(|c| c == '"' || c == '\\' || c.is_control())
}

pub fn is_identifier(s: &str) -> bool {
    (1..=128).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"._:@+-".contains(&b))
}

pub fn is_action(s: &str) -> bool {
    let parts: Vec<&str> = s.split('.').collect();
    s.len() <= 64
        && parts.len() >= 2
        && parts.iter().all(|p| {
            p.bytes().next().is_some_and(|b| b.is_ascii_lowercase())
                && p.bytes().all(|b| b.is_ascii_lowercase() || b.is_ascii_digit() || b == b'_')
        })
}

pub fn is_holder(s: &str) -> bool {
    s.split_once(':').is_some_and(|(kind, id)| HOLDER_KINDS.contains(&kind) && is_identifier(id))
}

pub fn is_issuer(s: &str) -> bool {
    s.strip_prefix("ed25519/").is_some_and(|hex| hex.len() == 64 && is_lower_hex(hex))
}

pub fn is_lower_hex(s: &str) -> bool {
    s.bytes().all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

pub fn is_revocation_id(s: &str) -> bool {
    s.len() == 128 && is_lower_hex(s)
}

pub fn is_folder(s: &str) -> bool {
    s.len() <= 1024
        && is_safe_text(s, 1024)
        && s.strip_prefix('/').is_some_and(|rest| rest.split('/').all(|seg| !seg.is_empty() && seg != "." && seg != ".."))
}

pub fn is_host(s: &str) -> bool {
    let (name, port) = match s.split_once(':') {
        Some((name, port)) => (name, Some(port)),
        None => (s, None),
    };
    let edge = |b: u8| b.is_ascii_lowercase() || b.is_ascii_digit();
    let bytes = name.as_bytes();
    (1..=253).contains(&bytes.len())
        && edge(bytes[0])
        && edge(bytes[bytes.len() - 1])
        && bytes.iter().all(|&b| edge(b) || b == b'.' || b == b'-')
        && port.is_none_or(|p| (1..=5).contains(&p.len()) && p.bytes().all(|b| b.is_ascii_digit()))
}

pub fn is_zone(s: &str) -> bool {
    (1..=64).contains(&s.len()) && s.bytes().all(|b| b.is_ascii_alphanumeric() || b"_+/-".contains(&b))
}

pub fn is_currency(s: &str) -> bool {
    (3..=10).contains(&s.len())
        && s.as_bytes()[0].is_ascii_uppercase()
        && s.bytes().all(|b| b.is_ascii_uppercase() || b.is_ascii_digit())
}

pub fn is_time(t: i64) -> bool {
    (0..=MAX_TIME).contains(&t)
}

/// `value` may be `*` only where `allow_any` (a permission); a request names one resource.
pub fn is_resource(r: &Resource, allow_any: bool) -> bool {
    if !RESOURCE_KINDS.contains(&r.kind.as_str()) {
        return false;
    }
    if r.value == "*" {
        return allow_any;
    }
    match r.kind.as_str() {
        "folder" => is_folder(&r.value),
        "host" => is_host(&r.value),
        _ => !r.value.is_empty() && is_safe_text(&r.value, 128),
    }
}

/// `spend` needs a currency; `requests` and `messages` have the empty unit.
pub fn is_counter(kind: &str, unit: &str) -> bool {
    match kind {
        "spend" => is_currency(unit),
        "requests" | "messages" => unit.is_empty(),
        _ => false,
    }
}

pub fn is_period(s: &str) -> bool {
    s == "day" || s == "month"
}

/// `read`, `write` or `spend`; a name the registry does not know is `write`.
pub fn action_class(action: &str) -> &'static str {
    if READ_ACTIONS.contains(&action) {
        "read"
    } else if SPEND_ACTIONS.contains(&action) {
        "spend"
    } else {
        "write"
    }
}

fn sorted_unique<T: Ord + Clone>(items: &[T]) -> Option<Vec<T>> {
    let set: BTreeSet<T> = items.iter().cloned().collect();
    (set.len() == items.len()).then(|| set.into_iter().collect())
}

fn valid_confirm(confirm: &Confirm, limits: &[(String, String, i64)]) -> bool {
    match confirm {
        Confirm::OverAmount { unit, decimals, amount } => {
            *amount >= 0 && limits.iter().any(|(kind, u, d)| kind == "spend" && u == unit && d == decimals)
        }
        _ => true,
    }
}

// ---------------------------------------------------------------- drafts

/// Validate the fields of a permission and return them in canonical order.
/// (Turning a person's free text into these fields is the helper's job, not this function's.)
pub fn propose_from_fields(fields: Draft) -> Result<Draft, PermissionError> {
    let mut d = fields;
    if !is_issuer(&d.issuer) {
        return fail("invalid_issuer", "issuer must be ed25519/<64 hex>");
    }
    if !is_holder(&d.holder) {
        return fail("invalid_holder", "holder must be <device|helper|app|clinician>:<id>");
    }
    d.actions = match sorted_unique(&d.actions) {
        Some(a) if (1..=32).contains(&a.len()) && a.iter().all(|x| is_action(x)) => a,
        _ => return fail("invalid_actions", "1 to 32 distinct dotted action names"),
    };
    d.routine = match sorted_unique(&d.routine) {
        Some(r) if r.iter().all(|x| d.actions.contains(x)) => r,
        _ => return fail("invalid_routine", "routine actions must be distinct and listed in actions"),
    };
    d.resources = match sorted_unique(&d.resources) {
        Some(r) if (1..=64).contains(&r.len()) && r.iter().all(|x| is_resource(x, true)) => r,
        _ => return fail("invalid_resources", "1 to 64 distinct resources of a known kind"),
    };
    d.limits = match sorted_unique(&d.limits) {
        Some(l) if l.len() <= 16 && valid_limits(&l) => l,
        _ => return fail("invalid_limits", "limits need a known kind, unit, period and a non-negative amount, once per key"),
    };
    if !is_safe_text(&d.purpose_text, 500) || !(d.purpose_code.is_empty() || (is_identifier(&d.purpose_code) && d.purpose_code.len() <= 64)) {
        return fail("invalid_purpose", "purpose text: no double quote, backslash or control character; code: an identifier");
    }
    if !(is_time(d.issued_at) && is_time(d.not_before) && is_time(d.not_after) && d.not_before < d.not_after) {
        return fail("invalid_validity", "not_before must be before not_after, both within years 1970 to 9999");
    }
    let keys: Vec<(String, String, i64)> = d.limits.iter().map(|l| (l.kind.clone(), l.unit.clone(), l.decimals)).collect();
    if !valid_confirm(&d.confirm, &keys) {
        return fail("invalid_confirm", "over_amount needs a spend limit in the same unit and decimals");
    }
    if d.confirm == Confirm::Never && !d.actions.iter().all(|a| action_class(a) == "read" || d.routine.contains(a)) {
        return fail("invalid_confirm", "never is only for read actions and actions marked routine");
    }
    if !is_zone(&d.period_zone) {
        return fail("invalid_zone", "period_zone must be an IANA zone name");
    }
    Ok(d)
}

fn valid_limits(limits: &[Limit]) -> bool {
    let mut keys = BTreeSet::new();
    limits.iter().all(|l| {
        is_counter(&l.kind, &l.unit)
            && is_period(&l.period)
            && l.amount >= 0
            && (0..=18).contains(&l.decimals)
            && (l.kind == "spend" || l.decimals == 0)
            && limits.iter().all(|m| m.kind != l.kind || m.unit != l.unit || m.decimals == l.decimals)
            && keys.insert((&l.kind, &l.unit, &l.period))
    })
}

/// Shape rules for one block (what it may say at all; whether it is narrower than a
/// given token is `narrow`'s question). Returns it in canonical order.
pub fn canonical_narrowing(n: &Narrowing, authority: &Draft) -> Result<Narrowing, PermissionError> {
    let bad = |what: &str| fail("invalid_narrowing", what.to_string());
    let mut out = n.clone();
    out.actions = match sorted_unique(&n.actions) {
        Some(a) if a.len() <= 32 && a.iter().all(|x| is_action(x)) => a,
        _ => return bad("actions"),
    };
    out.resources = match sorted_unique(&n.resources) {
        Some(r) if r.len() <= 64 && r.iter().all(|x| is_resource(x, true)) => r,
        _ => return bad("resources"),
    };
    let limited = |kind: &str, unit: &str| authority.limits.iter().any(|l| l.kind == kind && l.unit == unit);
    out.limits = match sorted_unique(&n.limits) {
        Some(l) => l,
        None => return bad("limits"),
    };
    let mut keys = BTreeSet::new();
    if !out.limits.iter().all(|l| limited(&l.kind, &l.unit) && is_period(&l.period) && l.amount >= 0 && keys.insert((&l.kind, &l.unit, &l.period))) {
        return bad("limits");
    }
    out.caps = match sorted_unique(&n.caps) {
        Some(c) => c,
        None => return bad("caps"),
    };
    let mut keys = BTreeSet::new();
    if !out.caps.iter().all(|c| limited(&c.kind, &c.unit) && c.amount >= 0 && keys.insert((&c.kind, &c.unit))) {
        return bad("caps");
    }
    if !n.not_before.is_none_or(is_time) || !n.not_after.is_none_or(is_time) {
        return bad("validity");
    }
    let keys: Vec<(String, String, i64)> = authority.limits.iter().map(|l| (l.kind.clone(), l.unit.clone(), l.decimals)).collect();
    match &n.confirm {
        Some(Confirm::Never) => return bad("confirm: never is not a narrowing"),
        Some(c) if !valid_confirm(c, &keys) => return bad("confirm"),
        _ => {}
    }
    for id in [&n.job, &n.tool, &n.card].into_iter().flatten() {
        if !is_identifier(id) {
            return bad("job, tool and card must be identifiers");
        }
    }
    if (n.tool.is_some() || n.card.is_some()) && n.not_after.is_none() {
        return bad("a block bound to a tool call or a card must carry an end time");
    }
    if out == Narrowing::default() {
        return fail("empty_narrowing", "a block must restrict something");
    }
    Ok(out)
}

/// Well-formedness of a request (step 5 of the check).
pub fn valid_request(r: &Request) -> bool {
    let mut keys = BTreeSet::new();
    is_holder(&r.holder)
        && is_action(&r.action)
        && is_resource(&r.resource, false)
        && Privacy::from_name(&r.privacy).is_some()
        && r.amounts.iter().all(|a| is_counter(&a.kind, &a.unit) && a.value >= 0 && keys.insert((&a.kind, &a.unit)))
        && [&r.job, &r.tool, &r.card].into_iter().flatten().all(|id| is_identifier(id))
}
