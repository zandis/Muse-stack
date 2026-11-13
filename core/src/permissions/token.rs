//! Permission <-> Biscuit token: the block text, issuing, reading back, narrowing, and
//! the authorizer that runs the token's own Datalog.
//!
//! One function per line format writes both the text Biscuit prints for it and the
//! parameterised source it is built from, so values never pass through string
//! formatting into Datalog. Reading goes the other way: parse the printed block, then
//! require that printing the parsed value gives the same bytes.

use std::collections::HashMap;
use std::str::FromStr;
use std::time::Duration;

use biscuit_auth::builder::Term;
use biscuit_auth::{AuthorizerBuilder, AuthorizerLimits, Biscuit, BiscuitBuilder, BlockBuilder, KeyPair, PrivateKey, PublicKey, UnverifiedBiscuit};
use sha2::{Digest, Sha256};

use super::model::*;
use crate::civil;

// ---------------------------------------------------------------- writing block text

/// Datalog being written twice: `text` as Biscuit prints it, `source` with `{pN}` where a string goes.
#[derive(Default)]
pub(crate) struct Code {
    pub text: String,
    pub source: String,
    pub params: HashMap<String, Term>,
}

impl Code {
    fn raw(&mut self, s: &str) -> &mut Self {
        self.text.push_str(s);
        self.source.push_str(s);
        self
    }

    fn string(&mut self, s: &str) -> &mut Self {
        let name = format!("p{}", self.params.len());
        self.text.push('"');
        self.text.push_str(s);
        self.text.push('"');
        self.source.push_str(&format!("{{{name}}}"));
        self.params.insert(name, Term::Str(s.to_string()));
        self
    }

    fn int(&mut self, n: i64) -> &mut Self {
        self.raw(&n.to_string())
    }

    fn date(&mut self, t: i64) -> &mut Self {
        self.raw(&rfc3339(t))
    }

    fn end(&mut self) -> &mut Self {
        self.raw(";\n")
    }
}

/// `2026-11-30T00:00:00Z`, the form Biscuit prints a date in.
pub fn rfc3339(t: i64) -> String {
    let (y, m, d) = civil::civil_from_days(t.div_euclid(86_400));
    let s = t.rem_euclid(86_400);
    format!("{y:04}-{m:02}-{d:02}T{:02}:{:02}:{:02}Z", s / 3600, s / 60 % 60, s % 60)
}

fn confirm_args(code: &mut Code, confirm: &Confirm) {
    match confirm {
        Confirm::OverAmount { unit, decimals, amount } => {
            code.string("over_amount").raw(", ").string(unit).raw(", ").int(*decimals).raw(", ").int(*amount);
        }
        other => {
            code.string(other.name()).raw(", ").string("").raw(", 0, 0");
        }
    }
}

/// The authority block for `draft`; the `mandate` line is left out when `id` is None
/// (that text is what the id is the hash of).
pub(crate) fn authority_code(d: &Draft, id: Option<&str>) -> Code {
    let mut c = Code::default();
    c.raw("schema(").string(SCHEMA_NAME).raw(", ").int(SCHEMA_VERSION).raw(")").end();
    if let Some(id) = id {
        c.raw("mandate(").string(id).raw(")").end();
    }
    c.raw("issuer(").string(&d.issuer).raw(")").end();
    c.raw("holder(").string(&d.holder).raw(")").end();
    c.raw("issued_at(").date(d.issued_at).raw(")").end();
    c.raw("valid_from(").date(d.not_before).raw(")").end();
    c.raw("valid_until(").date(d.not_after).raw(")").end();
    c.raw("period_zone(").string(&d.period_zone).raw(")").end();
    c.raw("privacy_max(").int(d.privacy.rank()).raw(")").end();
    c.raw("purpose(").string(&d.purpose_text).raw(", ").string(&d.purpose_code).raw(")").end();
    c.raw("confirm(");
    confirm_args(&mut c, &d.confirm);
    c.raw(")").end();
    for a in &d.actions {
        c.raw("action(").string(a).raw(")").end();
    }
    for a in &d.routine {
        c.raw("routine(").string(a).raw(")").end();
    }
    for r in &d.resources {
        c.raw("resource(").string(&r.kind).raw(", ").string(&r.value).raw(")").end();
    }
    for l in &d.limits {
        c.raw("limit(").string(&l.kind).raw(", ").string(&l.unit).raw(", ").int(l.decimals).raw(", ").string(&l.period).raw(", ").int(l.amount).raw(")").end();
    }
    c
}

/// The id of a draft: `m1_` + SHA-256 of its authority block text without the `mandate` line.
pub fn mandate_id(draft: &Draft) -> String {
    let digest = Sha256::digest(authority_code(draft, None).text.as_bytes());
    format!("m1_{}", hex(&digest))
}

pub(crate) fn hex(bytes: &[u8]) -> String {
    bytes.iter().map(|b| format!("{b:02x}")).collect()
}

/// The checks of one narrowing block, in canonical order.
pub(crate) fn narrowing_code(n: &Narrowing) -> Code {
    let mut c = Code::default();
    if !n.actions.is_empty() {
        c.raw("check if ");
        for (i, a) in n.actions.iter().enumerate() {
            c.raw(if i == 0 { "req_action(" } else { " or req_action(" }).string(a).raw(")");
        }
        c.end();
    }
    if !n.resources.is_empty() {
        c.raw("check if ");
        for (i, r) in n.resources.iter().enumerate() {
            c.raw(if i == 0 { "req_resource(" } else { " or req_resource(" }).string(&r.kind);
            if r.value == "*" {
                c.raw(", $v)");
            } else {
                c.raw(", ").string(&r.value).raw(")");
                if r.kind == "folder" {
                    c.raw(" or req_resource(").string("folder").raw(", $v), $v.starts_with(").string(&format!("{}/", r.value)).raw(")");
                }
            }
        }
        c.end();
    }
    for l in &n.limits {
        c.raw("check if no_amount(").string(&l.kind).raw(", ").string(&l.unit).raw(") or amount(").string(&l.kind).raw(", ").string(&l.unit);
        c.raw(", $a), used(").string(&l.kind).raw(", ").string(&l.unit).raw(", ").string(&l.period).raw(", $n), $n + $a <= ").int(l.amount).end();
    }
    for cap in &n.caps {
        c.raw("check if no_amount(").string(&cap.kind).raw(", ").string(&cap.unit).raw(") or amount(").string(&cap.kind).raw(", ").string(&cap.unit).raw(", $a), $a <= ").int(cap.amount).end();
    }
    if let Some(t) = n.not_before {
        c.raw("check if time_late($t), $t >= ").date(t).end();
    }
    if let Some(t) = n.not_after {
        c.raw("check if time($t), $t <= ").date(t).end();
    }
    if let Some(p) = n.privacy {
        c.raw("check if req_privacy($p), $p <= ").int(p.rank()).end();
    }
    match &n.confirm {
        Some(Confirm::OverAmount { unit, decimals, amount }) => {
            c.raw("check if confirm_applied(").string("over_amount").raw(", ").string(unit).raw(", ").int(*decimals).raw(", ").int(*amount).raw(")").end();
        }
        Some(other) => {
            c.raw("check if confirm_applied(").string(other.name()).raw(")").end();
        }
        None => {}
    }
    for (name, id) in [("req_job", &n.job), ("req_tool", &n.tool), ("req_card", &n.card)] {
        if let Some(id) = id {
            c.raw("check if ").raw(name).raw("(").string(id).raw(")").end();
        }
    }
    c
}

// ---------------------------------------------------------------- reading block text

/// A cursor over one printed line. A string is read up to the next double quote: honest
/// blocks never hold one inside a string (the text rules), and the caller re-prints what
/// was read and compares, so anything else is refused.
struct Cur<'a>(&'a str);

impl<'a> Cur<'a> {
    fn eat(&mut self, lit: &str) -> bool {
        match self.0.strip_prefix(lit) {
            Some(rest) => {
                self.0 = rest;
                true
            }
            None => false,
        }
    }

    fn string(&mut self) -> Option<String> {
        let rest = self.0.strip_prefix('"')?;
        let end = rest.find('"')?;
        self.0 = &rest[end + 1..];
        Some(rest[..end].to_string())
    }

    fn int(&mut self) -> Option<i64> {
        let len = self.0.bytes().take_while(u8::is_ascii_digit).count();
        let n = self.0[..len].parse().ok()?;
        self.0 = &self.0[len..];
        Some(n)
    }

    fn date(&mut self) -> Option<i64> {
        let s = self.0.get(..20)?;
        let b = s.as_bytes();
        let num = |from: usize, to: usize| s.get(from..to).filter(|x| x.bytes().all(|c| c.is_ascii_digit())).and_then(|x| x.parse::<i64>().ok());
        if !(b[4] == b'-' && b[7] == b'-' && b[10] == b'T' && b[13] == b':' && b[16] == b':' && b[19] == b'Z') {
            return None;
        }
        let (y, mo, d, h, mi, sec) = (num(0, 4)?, num(5, 7)?, num(8, 10)?, num(11, 13)?, num(14, 16)?, num(17, 19)?);
        if !((1..=12).contains(&mo) && (1..=civil::days_in_month(y, mo)).contains(&d) && h < 24 && mi < 60 && sec < 60) {
            return None;
        }
        self.0 = &self.0[20..];
        Some(civil::days_from_civil(y, mo, d) * 86_400 + h * 3600 + mi * 60 + sec)
    }
}

fn lines(text: &str) -> Option<Vec<&str>> {
    let body = text.strip_suffix('\n')?;
    Some(body.split('\n').collect())
}

fn parse_confirm(c: &mut Cur) -> Option<Confirm> {
    let name = c.string()?;
    c.eat(", ").then_some(())?;
    let unit = c.string()?;
    c.eat(", ").then_some(())?;
    let decimals = c.int()?;
    c.eat(", ").then_some(())?;
    let amount = c.int()?;
    match name.as_str() {
        "over_amount" => Some(Confirm::OverAmount { unit, decimals, amount }),
        "each_write" => Some(Confirm::EachWrite),
        "never" => Some(Confirm::Never),
        _ => None,
    }
}

/// Read an authority block back into (mandate id, draft). None if a line is not one this schema writes.
fn parse_authority(text: &str) -> Option<(String, Draft)> {
    let mut id = None;
    let mut d = Draft {
        issuer: String::new(),
        holder: String::new(),
        actions: vec![],
        routine: vec![],
        resources: vec![],
        limits: vec![],
        purpose_text: String::new(),
        purpose_code: String::new(),
        issued_at: -1,
        not_before: -1,
        not_after: -1,
        confirm: Confirm::EachWrite,
        privacy: Privacy::Local,
        period_zone: String::new(),
    };
    for line in lines(text)? {
        let mut c = Cur(line);
        if c.eat("schema(") {
            (c.string()? == SCHEMA_NAME && c.eat(", ") && c.int()? == SCHEMA_VERSION).then_some(())?;
        } else if c.eat("mandate(") {
            id = Some(c.string()?);
        } else if c.eat("issuer(") {
            d.issuer = c.string()?;
        } else if c.eat("holder(") {
            d.holder = c.string()?;
        } else if c.eat("issued_at(") {
            d.issued_at = c.date()?;
        } else if c.eat("valid_from(") {
            d.not_before = c.date()?;
        } else if c.eat("valid_until(") {
            d.not_after = c.date()?;
        } else if c.eat("period_zone(") {
            d.period_zone = c.string()?;
        } else if c.eat("privacy_max(") {
            d.privacy = Privacy::from_rank(c.int()?)?;
        } else if c.eat("purpose(") {
            d.purpose_text = c.string()?;
            c.eat(", ").then_some(())?;
            d.purpose_code = c.string()?;
        } else if c.eat("confirm(") {
            d.confirm = parse_confirm(&mut c)?;
        } else if c.eat("action(") {
            d.actions.push(c.string()?);
        } else if c.eat("routine(") {
            d.routine.push(c.string()?);
        } else if c.eat("resource(") {
            let kind = c.string()?;
            c.eat(", ").then_some(())?;
            d.resources.push(Resource { kind, value: c.string()? });
        } else if c.eat("limit(") {
            let kind = c.string()?;
            c.eat(", ").then_some(())?;
            let unit = c.string()?;
            c.eat(", ").then_some(())?;
            let decimals = c.int()?;
            c.eat(", ").then_some(())?;
            let period = c.string()?;
            c.eat(", ").then_some(())?;
            d.limits.push(Limit { kind, unit, decimals, period, amount: c.int()? });
        } else {
            return None;
        }
        (c.0 == ");").then_some(())?;
    }
    Some((id?, d))
}

/// Read one narrowing block. None if a line is not one this schema writes.
fn parse_narrowing(text: &str) -> Option<Narrowing> {
    let mut n = Narrowing::default();
    for line in lines(text)? {
        let mut c = Cur(line);
        if c.eat("check if req_action(") {
            loop {
                n.actions.push(c.string()?);
                if !c.eat(") or req_action(") {
                    break;
                }
            }
            (c.0 == ");").then_some(())?;
        } else if c.eat("check if req_resource(") {
            loop {
                let kind = c.string()?;
                if c.eat(", $v), $v.starts_with(") {
                    c.string()?; // the "under this folder" half; re-printing checks it
                } else if c.eat(", $v") {
                    n.resources.push(Resource { kind, value: "*".to_string() });
                } else {
                    c.eat(", ").then_some(())?;
                    n.resources.push(Resource { kind, value: c.string()? });
                }
                if !c.eat(") or req_resource(") {
                    break;
                }
            }
            (c.0 == ");").then_some(())?;
        } else if c.eat("check if no_amount(") {
            let kind = c.string()?;
            c.eat(", ").then_some(())?;
            let unit = c.string()?;
            c.eat(") or amount(").then_some(())?;
            c.string()?; // the same kind and unit again; re-printing checks them
            c.eat(", ").then_some(())?;
            c.string()?;
            if c.eat(", $a), $a <= ") {
                n.caps.push(Cap { kind, unit, amount: c.int()? });
            } else {
                c.eat(", $a), used(").then_some(())?;
                c.string()?;
                c.eat(", ").then_some(())?;
                c.string()?;
                c.eat(", ").then_some(())?;
                let period = c.string()?;
                c.eat(", $n), $n + $a <= ").then_some(())?;
                n.limits.push(NarrowLimit { kind, unit, period, amount: c.int()? });
            }
            (c.0 == ";").then_some(())?;
        } else if c.eat("check if time_late($t), $t >= ") {
            n.not_before = Some(c.date()?);
            (c.0 == ";").then_some(())?;
        } else if c.eat("check if time($t), $t <= ") {
            n.not_after = Some(c.date()?);
            (c.0 == ";").then_some(())?;
        } else if c.eat("check if req_privacy($p), $p <= ") {
            n.privacy = Some(Privacy::from_rank(c.int()?)?);
            (c.0 == ";").then_some(())?;
        } else if c.eat("check if confirm_applied(\"each_write\");") {
            n.confirm = Some(Confirm::EachWrite);
            c.0.is_empty().then_some(())?;
        } else if c.eat("check if confirm_applied(") {
            n.confirm = Some(parse_confirm(&mut c)?);
            (c.0 == ");").then_some(())?;
        } else if c.eat("check if req_job(") {
            n.job = Some(c.string()?);
            (c.0 == ");").then_some(())?;
        } else if c.eat("check if req_tool(") {
            n.tool = Some(c.string()?);
            (c.0 == ");").then_some(())?;
        } else if c.eat("check if req_card(") {
            n.card = Some(c.string()?);
            (c.0 == ");").then_some(())?;
        } else {
            return None;
        }
    }
    Some(n)
}

// ---------------------------------------------------------------- keys

pub fn parse_public_key(text: &str) -> Result<PublicKey, PermissionError> {
    if !is_issuer(text) {
        return fail("invalid_key", "a public key is ed25519/<64 hex>");
    }
    PublicKey::from_str(text).map_err(|e| PermissionError::new("invalid_key", e.to_string()))
}

pub fn parse_private_key(text: &str) -> Result<KeyPair, PermissionError> {
    let ok = text.strip_prefix("ed25519-private/").is_some_and(|h| h.len() == 64 && h.bytes().all(|b| b.is_ascii_hexdigit()));
    if !ok {
        return fail("invalid_key", "a private key is ed25519-private/<64 hex>");
    }
    let key = PrivateKey::from_str(text).map_err(|e| PermissionError::new("invalid_key", e.to_string()))?;
    Ok(KeyPair::from(&key))
}

/// The text form of the public half of a root key.
pub fn public_key_text(root: &KeyPair) -> String {
    root.public().to_string()
}

// ---------------------------------------------------------------- the token

/// A verified permission token: the signed bytes and what they say.
#[derive(Clone)]
pub struct Mandate {
    pub(crate) biscuit: Biscuit,
    pub id: String,
    pub draft: Draft,
    /// One entry per appended block, oldest first.
    pub levels: Vec<Narrowing>,
}

impl std::fmt::Debug for Mandate {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("Mandate").field("id", &self.id).field("draft", &self.draft).field("levels", &self.levels).finish()
    }
}

fn lib_err(code: &'static str) -> impl Fn(biscuit_auth::error::Token) -> PermissionError {
    move |e| PermissionError::new(code, e.to_string())
}

/// Sign a draft with the person's root key.
pub fn issue(draft: &Draft, root: &KeyPair) -> Result<Mandate, PermissionError> {
    let draft = propose_from_fields(draft.clone())?;
    if draft.issuer != public_key_text(root) {
        return fail("wrong_issuer", "the draft names a different root key");
    }
    let id = mandate_id(&draft);
    let code = authority_code(&draft, Some(&id));
    let biscuit = BiscuitBuilder::new()
        .code_with_params(&code.source, code.params, HashMap::new())
        .and_then(|b| b.build(root))
        .map_err(lib_err("library_error"))?;
    if biscuit.print_block_source(0).map_err(lib_err("library_error"))? != code.text {
        return fail("library_error", "the library printed the authority block differently");
    }
    Ok(Mandate { biscuit, id, draft, levels: vec![] })
}

impl Mandate {
    /// Decode and verify a token under `root`, and read what it says.
    pub fn parse(token: &str, root: &str) -> Result<Mandate, PermissionError> {
        let key = parse_public_key(root)?;
        let unverified = UnverifiedBiscuit::from_base64(token).map_err(lib_err("malformed_token"))?;
        let biscuit = unverified.verify(key).map_err(|e| PermissionError::new("bad_signature", e.to_string()))?;
        Mandate::read(biscuit, root)
    }

    fn read(biscuit: Biscuit, root: &str) -> Result<Mandate, PermissionError> {
        let source = |i: usize| biscuit.print_block_source(i).map_err(lib_err("malformed_token"));
        let authority = source(0)?;
        if !authority.starts_with("schema(\"muse.permission\", 1);\n") {
            return fail("unsupported_schema", "not a muse.permission schema 1 token");
        }
        let (id, parsed) = parse_authority(&authority).ok_or_else(|| PermissionError::new("invalid_mandate", "the authority block is not in the schema's form"))?;
        let draft = propose_from_fields(parsed.clone()).map_err(|e| PermissionError::new("invalid_mandate", e.to_string()))?;
        if draft != parsed || id != mandate_id(&draft) || authority_code(&draft, Some(&id)).text != authority {
            return fail("invalid_mandate", "the authority block is not canonical or its id does not match");
        }
        if draft.issuer != root {
            return fail("wrong_issuer", "the token names a different root key");
        }
        let mut levels = Vec::new();
        for i in 1..biscuit.block_count() {
            let unknown = || PermissionError::new("unknown_block", format!("block {i} is not a schema 1 narrowing"));
            if biscuit.block_external_key(i).map_err(lib_err("malformed_token"))?.is_some() {
                return Err(unknown());
            }
            let text = source(i)?;
            let parsed = parse_narrowing(&text).ok_or_else(unknown)?;
            let level = canonical_narrowing(&parsed, &draft).map_err(|_| unknown())?;
            if level != parsed || narrowing_code(&level).text != text {
                return Err(unknown());
            }
            levels.push(level);
        }
        Ok(Mandate { biscuit, id, draft, levels })
    }

    pub fn to_base64(&self) -> Result<String, PermissionError> {
        self.biscuit.to_base64().map_err(lib_err("library_error"))
    }

    /// Size of the token in bytes (before base64).
    pub fn size(&self) -> usize {
        self.biscuit.serialized_size().unwrap_or(0)
    }

    // -------- the effective permission: authority and every block together

    pub fn not_before(&self) -> i64 {
        self.levels.iter().filter_map(|n| n.not_before).fold(self.draft.not_before, i64::max)
    }

    pub fn not_after(&self) -> i64 {
        self.levels.iter().filter_map(|n| n.not_after).fold(self.draft.not_after, i64::min)
    }

    pub fn privacy(&self) -> Privacy {
        self.levels.iter().filter_map(|n| n.privacy).fold(self.draft.privacy, Privacy::min)
    }

    pub fn actions(&self) -> Vec<String> {
        let mut out = self.draft.actions.clone();
        for n in self.levels.iter().filter(|n| !n.actions.is_empty()) {
            out.retain(|a| n.actions.contains(a));
        }
        out
    }

    pub fn resources(&self) -> Vec<Resource> {
        let mut out = self.draft.resources.clone();
        for n in self.levels.iter().filter(|n| !n.resources.is_empty()) {
            let mut next: Vec<Resource> = out.iter().flat_map(|a| n.resources.iter().filter_map(|b| a.intersect(b))).collect();
            next.sort();
            next.dedup();
            out = next;
        }
        out
    }

    /// Every limit that applies: the authority's, lowered where a block is lower, plus
    /// limits a block adds for a new period.
    pub fn limits(&self) -> Vec<Limit> {
        let mut out = self.draft.limits.clone();
        for l in self.levels.iter().flat_map(|n| &n.limits) {
            match out.iter_mut().find(|o| o.kind == l.kind && o.unit == l.unit && o.period == l.period) {
                Some(o) => o.amount = o.amount.min(l.amount),
                None => out.push(Limit { kind: l.kind.clone(), unit: l.unit.clone(), decimals: self.decimals(&l.kind, &l.unit), period: l.period.clone(), amount: l.amount }),
            }
        }
        out.sort();
        out
    }

    pub fn caps(&self) -> Vec<Cap> {
        let mut out: Vec<Cap> = Vec::new();
        for c in self.levels.iter().flat_map(|n| &n.caps) {
            match out.iter_mut().find(|o| o.kind == c.kind && o.unit == c.unit) {
                Some(o) => o.amount = o.amount.min(c.amount),
                None => out.push(c.clone()),
            }
        }
        out.sort();
        out
    }

    pub fn decimals(&self, kind: &str, unit: &str) -> i64 {
        self.draft.limits.iter().find(|l| l.kind == kind && l.unit == unit).map_or(0, |l| l.decimals)
    }

    /// Every confirmation rule in the chain, the authority's first.
    pub fn confirm_rules(&self) -> Vec<Confirm> {
        std::iter::once(self.draft.confirm.clone()).chain(self.levels.iter().filter_map(|n| n.confirm.clone())).collect()
    }

    fn binding(&self, pick: impl Fn(&Narrowing) -> &Option<String>) -> Vec<String> {
        let mut ids: Vec<String> = self.levels.iter().filter_map(|n| pick(n).clone()).collect();
        ids.sort();
        ids.dedup();
        ids
    }

    /// Ids this token is bound to. More than one distinct id means no request can match.
    pub fn jobs(&self) -> Vec<String> {
        self.binding(|n| &n.job)
    }

    pub fn tools(&self) -> Vec<String> {
        self.binding(|n| &n.tool)
    }

    pub fn cards(&self) -> Vec<String> {
        self.binding(|n| &n.card)
    }
}

/// One revocation identifier per block, as hex; the first is the root's.
pub fn revocation_ids(mandate: &Mandate) -> Vec<String> {
    mandate.biscuit.revocation_identifiers().iter().map(|id| hex(id)).collect()
}

/// The same, read from a token that has not been verified (so that a revoked token
/// is refused before anything else is looked at).
pub fn unverified_revocation_ids(token: &str) -> Result<Vec<String>, PermissionError> {
    let unverified = UnverifiedBiscuit::from_base64(token).map_err(lib_err("malformed_token"))?;
    Ok(unverified.revocation_identifiers().iter().map(|id| hex(id)).collect())
}

/// Append one block. Needs no key: anyone holding the token can do this offline.
/// Refuses a narrowing that is not inside what the token already allows.
pub fn narrow(mandate: &Mandate, narrowing: &Narrowing) -> Result<Mandate, PermissionError> {
    let n = canonical_narrowing(narrowing, &mandate.draft)?;
    let wider = |what: &str| fail("not_narrower", what.to_string());
    let actions = mandate.actions();
    if !n.actions.iter().all(|a| actions.contains(a)) {
        return wider("an action the token does not allow");
    }
    let covered = |r: &Resource| mandate.draft.resources.iter().any(|p| p.covers(r)) && mandate.levels.iter().all(|l| l.resources.is_empty() || l.resources.iter().any(|p| p.covers(r)));
    if !n.resources.iter().all(covered) {
        return wider("a resource the token does not cover");
    }
    let limits = mandate.limits();
    for l in &n.limits {
        if limits.iter().any(|p| p.kind == l.kind && p.unit == l.unit && p.period == l.period && l.amount > p.amount) {
            return wider("a limit above the token's");
        }
    }
    let caps = mandate.caps();
    for c in &n.caps {
        if caps.iter().any(|p| p.kind == c.kind && p.unit == c.unit && c.amount > p.amount) {
            return wider("a cap above the token's");
        }
    }
    let (start, end) = (n.not_before.unwrap_or(mandate.not_before()), n.not_after.unwrap_or(mandate.not_after()));
    if start < mandate.not_before() || end > mandate.not_after() || start > end {
        return wider("validity outside the token's");
    }
    if n.privacy.is_some_and(|p| p > mandate.privacy()) {
        return wider("a less protective privacy class");
    }
    for (new, old) in [(&n.job, mandate.jobs()), (&n.tool, mandate.tools()), (&n.card, mandate.cards())] {
        if new.as_ref().is_some_and(|id| old.iter().any(|o| o != id)) {
            return wider("already bound to a different job, tool call or card");
        }
    }
    let code = narrowing_code(&n);
    let block = BlockBuilder::new().code_with_params(&code.source, code.params, HashMap::new()).map_err(lib_err("library_error"))?;
    let biscuit = mandate.biscuit.append(block).map_err(|e| match e {
        biscuit_auth::error::Token::AppendOnSealed | biscuit_auth::error::Token::AlreadySealed => PermissionError::new("sealed_token", "a sealed token cannot be narrowed"),
        other => PermissionError::new("library_error", other.to_string()),
    })?;
    if biscuit.print_block_source(biscuit.block_count() - 1).map_err(lib_err("library_error"))? != code.text {
        return fail("library_error", "the library printed the block differently");
    }
    let mut levels = mandate.levels.clone();
    levels.push(n);
    Ok(Mandate { biscuit, id: mandate.id.clone(), draft: mandate.draft.clone(), levels })
}

// ---------------------------------------------------------------- the authorizer

/// The fixed policy of schema 1 (interface document, section 5.2).
pub const AUTHORIZER_POLICY: &str = r#"check if schema("muse.permission", 1);
check if holder($h), req_holder($h);
check if valid_from($d), time_late($t), $t >= $d;
check if valid_until($d), time($t), $t <= $d;
check if action($a), req_action($a);
check if req_resource($k, $v), resource($k, $v) or req_resource($k, $v), resource($k, "*") or req_resource("folder", $v), resource("folder", $p), $v.starts_with($p + "/");
check if privacy_max($m), req_privacy($p), $p <= $m;
allow if mandate($id);
"#;

/// Added to the policy once per limit in the authority block. (`check all` cannot be used:
/// in Biscuit it fails when nothing matches, and most requests carry no amount.)
pub const AUTHORIZER_LIMIT_CHECK: &str = "check if no_amount({kind}, {unit}) or limit({kind}, {unit}, $d, {period}, $max), amount({kind}, {unit}, $a), used({kind}, {unit}, {period}, $n), $n + $a <= $max;\n";

/// The request as authorizer facts.
fn request_code(req: &Request, limits: &[Limit], usage: &[(String, String, String, i64)], rules: &[Confirm], now: i64, skew: i64) -> Code {
    let mut c = Code::default();
    c.raw("time(").date((now - skew).clamp(0, MAX_TIME)).raw(")").end();
    c.raw("time_late(").date((now + skew).clamp(0, MAX_TIME)).raw(")").end();
    c.raw("req_holder(").string(&req.holder).raw(")").end();
    c.raw("req_action(").string(&req.action).raw(")").end();
    c.raw("req_resource(").string(&req.resource.kind).raw(", ").string(&req.resource.value).raw(")").end();
    c.raw("req_privacy(").int(Privacy::from_name(&req.privacy).map_or(99, Privacy::rank)).raw(")").end();
    for (name, id) in [("req_job", &req.job), ("req_tool", &req.tool), ("req_card", &req.card)] {
        if let Some(id) = id {
            c.raw(name).raw("(").string(id).raw(")").end();
        }
    }
    for a in &req.amounts {
        c.raw("amount(").string(&a.kind).raw(", ").string(&a.unit).raw(", ").int(a.value).raw(")").end();
    }
    let mut counters: Vec<(&String, &String)> = limits.iter().map(|l| (&l.kind, &l.unit)).collect();
    counters.dedup();
    for (kind, unit) in counters {
        if !req.amounts.iter().any(|a| a.kind == *kind && a.unit == *unit) {
            c.raw("no_amount(").string(kind).raw(", ").string(unit).raw(")").end();
        }
    }
    for (kind, unit, period, used) in usage {
        c.raw("used(").string(kind).raw(", ").string(unit).raw(", ").string(period).raw(", ").int(*used).raw(")").end();
    }
    for rule in rules {
        match rule {
            Confirm::OverAmount { unit, decimals, amount } => {
                c.raw("confirm_applied(").string("over_amount").raw(", ").string(unit).raw(", ").int(*decimals).raw(", ").int(*amount).raw(")").end();
            }
            other => {
                c.raw("confirm_applied(").string(other.name()).raw(")").end();
            }
        }
    }
    c
}

/// Run the token's own Datalog against the request. True only if the library allows.
/// This is the second, independent gate of `check`; it is public so tests can ask the
/// library alone.
pub fn engine_allows(mandate: &Mandate, req: &Request, usage: &[(String, String, String, i64)], rules: &[Confirm], now: i64, skew: i64) -> bool {
    let code = request_code(req, &mandate.draft.limits, usage, rules, now, skew);
    let limits = AuthorizerLimits { max_facts: 10_000, max_iterations: 1_000, max_time: Duration::from_secs(2) };
    let mut per_limit = AuthorizerBuilder::new();
    for l in &mandate.draft.limits {
        let params = HashMap::from([("kind".to_string(), Term::Str(l.kind.clone())), ("unit".to_string(), Term::Str(l.unit.clone())), ("period".to_string(), Term::Str(l.period.clone()))]);
        per_limit = match per_limit.code_with_params(AUTHORIZER_LIMIT_CHECK, params, HashMap::new()) {
            Ok(builder) => builder,
            Err(_) => return false,
        };
    }
    let run = || -> Result<usize, biscuit_auth::error::Token> {
        AuthorizerBuilder::new()
            .code_with_params(&code.source, code.params.clone(), HashMap::new())?
            .code(AUTHORIZER_POLICY)?
            .merge(per_limit.clone())
            .set_limits(limits.clone())
            .build(&mandate.biscuit)?
            .authorize()
    };
    run().is_ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn dates_round_trip() {
        for t in [0, 1, 86_399, 86_400, 951_782_400, 1_794_528_000, MAX_TIME] {
            let text = rfc3339(t);
            let mut c = Cur(&text);
            assert_eq!(c.date(), Some(t), "{text}");
            assert!(c.0.is_empty());
        }
        assert_eq!(rfc3339(1_794_528_000), "2026-11-13T00:00:00Z");
        assert_eq!(Cur("2026-02-30T00:00:00Z").date(), None);
        assert_eq!(Cur("2026-11-13 00:00:00Z").date(), None);
    }
}
