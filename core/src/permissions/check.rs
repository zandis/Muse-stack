//! Deciding one request (interface document, section 5). Pure: the clock, the usage
//! figures, the revocation list and the spent set are all arguments.

use std::collections::BTreeMap;

use super::lists::{RevocationList, SpentSet};
use super::model::*;
use super::token::{engine_allows, revocation_ids, unverified_revocation_ids, Mandate};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Decision {
    Allow,
    /// Refused; the string is one of the reasons in the interface document.
    Deny(String),
    /// Allowed only once the person approves; the string names the rule that asks.
    NeedsConfirmation(String),
}

impl Decision {
    fn deny(reason: &str) -> Decision {
        Decision::Deny(reason.to_string())
    }
}

/// "Used so far this period" for (kind, unit, period), supplied by the enforcing module
/// from its ledger for this mandate id. The token cannot check these figures.
#[derive(Debug, Clone, Default, PartialEq, Eq)]
pub struct Usage(pub BTreeMap<(String, String, String), i64>);

impl Usage {
    pub fn new() -> Usage {
        Usage::default()
    }

    pub fn set(&mut self, kind: &str, unit: &str, period: &str, used: i64) -> &mut Self {
        self.0.insert((kind.to_string(), unit.to_string(), period.to_string()), used);
        self
    }

    pub fn get(&self, kind: &str, unit: &str, period: &str) -> Option<i64> {
        self.0.get(&(kind.to_string(), unit.to_string(), period.to_string())).copied()
    }
}

/// The key a single-use id is remembered under.
pub fn spent_key(mandate_id: &str, kind: &str, id: &str) -> String {
    format!("{mandate_id}/{kind}/{id}")
}

/// The single-use ids a request spends when it is allowed, with the time each may be forgotten.
pub fn single_use_keys(mandate: &Mandate, skew: i64) -> Vec<(String, i64)> {
    let until = mandate.not_after().saturating_add(skew.clamp(0, MAX_SKEW));
    let tools = mandate.tools().into_iter().map(|id| (spent_key(&mandate.id, "tool", &id), until));
    let cards = mandate.cards().into_iter().map(|id| (spent_key(&mandate.id, "card", &id), until));
    tools.chain(cards).collect()
}

/// For each block that binds a tool call or a card: the end time in force at that block.
/// A later block cannot change it, so shortening a token afterwards never turns
/// `single_use_too_long` into allow.
fn single_use_ends(m: &Mandate) -> Vec<i64> {
    let mut end = m.draft.not_after;
    let mut out = Vec::new();
    for level in &m.levels {
        end = end.min(level.not_after.unwrap_or(end));
        if level.tool.is_some() || level.card.is_some() {
            out.push(end);
        }
    }
    out
}

/// `check` with the default clock tolerance of 60 seconds.
pub fn check(mandate: &Mandate, request: &Request, usage: &Usage, revoked: &RevocationList, spent: &SpentSet, now: i64) -> Decision {
    check_with_skew(mandate, request, usage, revoked, spent, now, DEFAULT_SKEW)
}

pub fn check_with_skew(m: &Mandate, req: &Request, usage: &Usage, revoked: &RevocationList, spent: &SpentSet, now: i64, skew: i64) -> Decision {
    let skew = skew.clamp(0, MAX_SKEW);
    if revocation_ids(m).iter().any(|id| revoked.contains(id)) {
        return Decision::deny("revoked");
    }
    if !valid_request(req) || !is_time(now) {
        return Decision::deny("bad_request");
    }
    if req.holder != m.draft.holder {
        return Decision::deny("wrong_holder");
    }
    if now + skew < m.not_before() {
        return Decision::deny("not_yet_valid");
    }
    if now - skew > m.not_after() {
        return Decision::deny("expired");
    }
    if !m.draft.actions.contains(&req.action) || !m.levels.iter().all(|l| l.actions.is_empty() || l.actions.contains(&req.action)) {
        return Decision::deny("action_not_allowed");
    }
    let covers = |list: &[Resource]| list.iter().any(|r| r.covers(&req.resource));
    if !covers(&m.draft.resources) || !m.levels.iter().all(|l| l.resources.is_empty() || covers(&l.resources)) {
        return Decision::deny("resource_not_allowed");
    }
    let privacy = Privacy::from_name(&req.privacy).expect("validated above");
    if privacy > m.privacy() {
        return Decision::deny("privacy_not_allowed");
    }
    for (bound, given, reason) in [(m.jobs(), &req.job, "wrong_job"), (m.tools(), &req.tool, "wrong_tool"), (m.cards(), &req.card, "wrong_card")] {
        if !bound.iter().all(|id| Some(id) == given.as_ref()) {
            return Decision::deny(reason);
        }
    }
    let single_use = single_use_keys(m, skew);
    if !single_use.is_empty() {
        if single_use_ends(m).iter().any(|end| end - now > MAX_SINGLE_USE_TTL + skew) {
            return Decision::deny("single_use_too_long");
        }
        if single_use.iter().any(|(key, _)| spent.contains(key, now)) {
            return Decision::deny("already_used");
        }
    }
    let class = action_class(&req.action);
    if class == "spend" && !req.amounts.iter().any(|a| a.kind == "spend") {
        return Decision::deny("amount_required");
    }
    let limits = m.limits();
    let mut used_facts = Vec::new();
    for a in &req.amounts {
        if !m.draft.limits.iter().any(|l| l.kind == a.kind && l.unit == a.unit) {
            return Decision::deny("limit_missing");
        }
        for l in limits.iter().filter(|l| l.kind == a.kind && l.unit == a.unit) {
            let Some(used) = usage.get(&l.kind, &l.unit, &l.period).filter(|u| *u >= 0) else {
                return Decision::deny("usage_missing");
            };
            if used.checked_add(a.value).is_none_or(|total| total > l.amount) {
                return Decision::deny("over_limit");
            }
            used_facts.push((l.kind.clone(), l.unit.clone(), l.period.clone(), used));
        }
        if m.caps().iter().any(|c| c.kind == a.kind && c.unit == a.unit && a.value > c.amount) {
            return Decision::deny("over_cap");
        }
    }
    let rules = m.confirm_rules();
    let approved = !m.cards().is_empty();
    if !approved {
        if let Some(rule) = asks(&rules, class, m.draft.routine.contains(&req.action), &req.amounts) {
            return Decision::NeedsConfirmation(rule.to_string());
        }
    }
    if !engine_allows(m, req, &used_facts, &rules, now, skew) {
        return Decision::deny("engine_disagreement");
    }
    Decision::Allow
}

/// Which confirmation rule, if any, asks for this request (section 5.1).
fn asks(rules: &[Confirm], class: &str, routine: bool, amounts: &[Amount]) -> Option<&'static str> {
    let mut over = false;
    for rule in rules {
        match rule {
            Confirm::Never => {}
            Confirm::EachWrite => {
                if class != "read" && !routine {
                    return Some("each_write");
                }
            }
            Confirm::OverAmount { unit, amount, .. } => {
                if class == "write" && !routine {
                    return Some("each_write");
                }
                if class == "spend" && amounts.iter().any(|a| a.kind == "spend" && (a.unit != *unit || a.value > *amount)) {
                    over = true;
                }
            }
        }
    }
    over.then_some("over_amount")
}

/// `check` from the encoded token: decoding, revocation, signature and structure first.
#[allow(clippy::too_many_arguments)]
pub fn check_encoded(token: &str, root: &str, request: &Request, usage: &Usage, revoked: &RevocationList, spent: &SpentSet, now: i64, skew: i64) -> Decision {
    let ids = match unverified_revocation_ids(token) {
        Ok(ids) => ids,
        Err(_) => return Decision::deny("malformed_token"),
    };
    if ids.iter().any(|id| revoked.contains(id)) {
        return Decision::deny("revoked");
    }
    match Mandate::parse(token, root) {
        Ok(mandate) => check_with_skew(&mandate, request, usage, revoked, spent, now, skew),
        Err(e) => Decision::deny(e.code),
    }
}

/// `check`, and when the answer is allow, remember the token's single-use ids.
/// A full spent set refuses the request: forgetting a live id would allow a replay.
pub fn check_and_record(mandate: &Mandate, request: &Request, usage: &Usage, revoked: &RevocationList, spent: &mut SpentSet, now: i64, skew: i64) -> Decision {
    let decision = check_with_skew(mandate, request, usage, revoked, spent, now, skew);
    if decision == Decision::Allow {
        let keys = single_use_keys(mandate, skew);
        if !spent.has_room(keys.len(), now) {
            return Decision::deny("spent_set_full");
        }
        for (key, until) in keys {
            spent.insert(&key, until, now).expect("room was checked");
        }
    }
    decision
}
