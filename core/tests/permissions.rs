//! Behaviour and property tests for `muse_core::permissions`.
//!
//! The property tests use a small fixed-seed generator (no extra dependency), so a
//! failure names a seed that reproduces it.

use std::collections::HashMap;

use biscuit_auth::{Biscuit, BlockBuilder};
use muse_core::permissions::*;

pub const TEST_ROOT: &str = "ed25519-private/0101010101010101010101010101010101010101010101010101010101010101";
const T0: i64 = 1_794_528_000; // 2026-11-13T00:00:00Z
const DAY: i64 = 86_400;

fn root() -> KeyPair {
    parse_private_key(TEST_ROOT).unwrap()
}

fn res(kind: &str, value: &str) -> Resource {
    Resource::new(kind, value)
}

fn s(items: &[&str]) -> Vec<String> {
    items.iter().map(|x| x.to_string()).collect()
}

fn base_draft() -> Draft {
    Draft {
        issuer: public_key_text(&root()),
        holder: "helper:vm".into(),
        actions: s(&["files.read", "files.write", "pay.spend"]),
        routine: vec![],
        resources: vec![res("folder", "/docs/work"), res("host", "api.example.com")],
        limits: vec![
            Limit { kind: "spend".into(), unit: "TWD".into(), decimals: 2, period: "day".into(), amount: 2000 },
            Limit { kind: "requests".into(), unit: "".into(), decimals: 0, period: "day".into(), amount: 200 },
        ],
        purpose_text: "Weekly digest 每週摘要".into(),
        purpose_code: "".into(),
        issued_at: T0,
        not_before: T0,
        not_after: T0 + 17 * DAY,
        confirm: Confirm::EachWrite,
        privacy: Privacy::Private,
        period_zone: "Asia/Taipei".into(),
    }
}

fn request(action: &str, resource: Resource) -> Request {
    Request { holder: "helper:vm".into(), action: action.into(), resource, amounts: vec![], privacy: "private".into(), job: None, tool: None, card: None }
}

fn spend(value: i64) -> Vec<Amount> {
    vec![Amount { kind: "spend".into(), unit: "TWD".into(), value }]
}

fn usage(spent_today: i64) -> Usage {
    let mut u = Usage::new();
    u.set("spend", "TWD", "day", spent_today).set("requests", "", "day", 0);
    u
}

fn decide(m: &Mandate, r: &Request, now: i64) -> Decision {
    check(m, r, &usage(0), &RevocationList::new(), &SpentSet::default(), now)
}

fn deny(reason: &str) -> Decision {
    Decision::Deny(reason.to_string())
}

fn confirm(rule: &str) -> Decision {
    Decision::NeedsConfirmation(rule.to_string())
}

// ---------------------------------------------------------------- the flow

#[test]
fn issue_parse_and_read() {
    let m = issue(&base_draft(), &root()).unwrap();
    assert!(m.id.starts_with("m1_") && m.id.len() == 67);
    assert_eq!(m.id, mandate_id(&m.draft));
    let again = Mandate::parse(&m.to_base64().unwrap(), &m.draft.issuer).unwrap();
    assert_eq!(again.draft, m.draft);
    assert_eq!(again.id, m.id);
    assert_eq!(revocation_ids(&again), revocation_ids(&m));
    assert_eq!(revocation_ids(&m).len(), 1);
    assert!(m.size() < 1200, "authority block grew to {}", m.size());

    let read = request("files.read", res("folder", "/docs/work/a.md"));
    assert_eq!(decide(&m, &read, T0 + 5), Decision::Allow);
    let write = request("files.write", res("folder", "/docs/work/a.md"));
    assert_eq!(decide(&m, &write, T0 + 5), confirm("each_write"));
}

#[test]
fn two_issues_of_one_draft_differ_in_bytes_but_not_in_id() {
    let a = issue(&base_draft(), &root()).unwrap();
    let b = issue(&base_draft(), &root()).unwrap();
    assert_eq!(a.id, b.id);
    assert_ne!(a.to_base64().unwrap(), b.to_base64().unwrap(), "each block is signed with a fresh random key");
    assert_ne!(revocation_ids(&a), revocation_ids(&b));
}

#[test]
fn every_deny_reason() {
    let m = issue(&base_draft(), &root()).unwrap();
    let ok = request("files.read", res("folder", "/docs/work"));
    assert_eq!(decide(&m, &ok, T0), Decision::Allow);

    let mut r = ok.clone();
    r.holder = "helper:other".into();
    assert_eq!(decide(&m, &r, T0), deny("wrong_holder"));
    r = ok.clone();
    r.privacy = "nonsense".into();
    assert_eq!(decide(&m, &r, T0), deny("bad_request"));
    r = ok.clone();
    r.resource = res("folder", "/docs/work/../x");
    assert_eq!(decide(&m, &r, T0), deny("bad_request"));
    assert_eq!(decide(&m, &ok, T0 - 61), deny("not_yet_valid"));
    assert_eq!(decide(&m, &ok, T0 - 60), Decision::Allow);
    assert_eq!(decide(&m, &ok, T0 + 17 * DAY + 60), Decision::Allow);
    assert_eq!(decide(&m, &ok, T0 + 17 * DAY + 61), deny("expired"));
    assert_eq!(decide(&m, &request("mail.send", res("folder", "/docs/work")), T0), deny("action_not_allowed"));
    for path in ["/docs/workshop", "/docs/work2/a", "/docs", "/docs/wor", "/other"] {
        assert_eq!(decide(&m, &request("files.read", res("folder", path)), T0), deny("resource_not_allowed"), "{path}");
    }
    assert_eq!(decide(&m, &request("files.read", res("host", "evil.example.com")), T0), deny("resource_not_allowed"));
    assert_eq!(decide(&m, &request("files.read", res("calendar", "work")), T0), deny("resource_not_allowed"));
    r = ok.clone();
    r.privacy = "standard".into();
    assert_eq!(decide(&m, &r, T0), deny("privacy_not_allowed"));
    r.privacy = "local".into();
    assert_eq!(decide(&m, &r, T0), Decision::Allow);

    // amounts
    let pay = |amount: Vec<Amount>| Request { amounts: amount, ..request("pay.spend", res("host", "api.example.com")) };
    assert_eq!(decide(&m, &pay(vec![]), T0), deny("amount_required"));
    assert_eq!(decide(&m, &pay(vec![Amount { kind: "spend".into(), unit: "USD".into(), value: 1 }]), T0), deny("limit_missing"));
    let none = Usage::new();
    assert_eq!(check(&m, &pay(spend(1)), &none, &RevocationList::new(), &SpentSet::default(), T0), deny("usage_missing"));
    assert_eq!(check(&m, &pay(spend(1)), &usage(2000), &RevocationList::new(), &SpentSet::default(), T0), deny("over_limit"));
    assert_eq!(check(&m, &pay(spend(1)), &usage(i64::MAX), &RevocationList::new(), &SpentSet::default(), T0), deny("over_limit"));
    assert_eq!(check(&m, &pay(spend(1)), &usage(1999), &RevocationList::new(), &SpentSet::default(), T0), confirm("each_write"));
    let capped = narrow(&m, &Narrowing { caps: vec![Cap { kind: "spend".into(), unit: "TWD".into(), amount: 100 }], ..Default::default() }).unwrap();
    assert_eq!(decide(&capped, &pay(spend(101)), T0), deny("over_cap"));
    assert_eq!(decide(&capped, &pay(spend(100)), T0), confirm("each_write"));

    // revocation, at the root and at a block
    let narrowed = narrow(&m, &Narrowing { actions: s(&["files.read"]), ..Default::default() }).unwrap();
    let deeper = narrow(&narrowed, &Narrowing { job: Some("job-1".into()), ..Default::default() }).unwrap();
    let job = Request { job: Some("job-1".into()), ..ok.clone() };
    let mut list = RevocationList::new();
    list.add(&revocation_ids(&narrowed)[1], T0, &root()).unwrap();
    let with = |m: &Mandate, r: &Request, l: &RevocationList| check(m, r, &usage(0), l, &SpentSet::default(), T0);
    assert_eq!(with(&m, &ok, &list), Decision::Allow, "revoking a narrowing leaves its parent alone");
    assert_eq!(with(&narrowed, &ok, &list), deny("revoked"));
    assert_eq!(with(&deeper, &job, &list), deny("revoked"), "and takes everything narrowed from it");
    let mut family = RevocationList::new();
    family.add(&revocation_ids(&m)[0], T0, &root()).unwrap();
    for t in [&m, &narrowed, &deeper] {
        assert_eq!(with(t, &job, &family), deny("revoked"));
    }

    // bindings
    assert_eq!(decide(&deeper, &ok, T0), deny("wrong_job"));
    assert_eq!(decide(&deeper, &Request { job: Some("job-2".into()), ..ok.clone() }, T0), deny("wrong_job"));
    assert_eq!(decide(&deeper, &job, T0), Decision::Allow);
    let tool = narrow(&deeper, &Narrowing { tool: Some("call-9".into()), not_after: Some(T0 + 600), ..Default::default() }).unwrap();
    assert_eq!(decide(&tool, &job, T0), deny("wrong_tool"));
    let call = Request { tool: Some("call-9".into()), ..job.clone() };
    assert_eq!(decide(&tool, &call, T0), Decision::Allow);
    let long = narrow(&deeper, &Narrowing { tool: Some("call-9".into()), not_after: Some(T0 + 2 * DAY), ..Default::default() }).unwrap();
    assert_eq!(decide(&long, &call, T0), deny("single_use_too_long"));
}

#[test]
fn encoded_tokens_fail_closed() {
    let m = issue(&base_draft(), &root()).unwrap();
    let token = m.to_base64().unwrap();
    let pk = m.draft.issuer.clone();
    let ok = request("files.read", res("folder", "/docs/work"));
    let run = |token: &str, root: &str| check_encoded(token, root, &ok, &usage(0), &RevocationList::new(), &SpentSet::default(), T0, 60);
    assert_eq!(run(&token, &pk), Decision::Allow);
    assert_eq!(run("not a token", &pk), deny("malformed_token"));
    assert_eq!(run("", &pk), deny("malformed_token"));
    let other = public_key_text(&parse_private_key(&TEST_ROOT.replace("01", "02")).unwrap());
    assert_eq!(run(&token, &other), deny("bad_signature"));
    // a flipped byte in the middle of the token
    let mut bytes = token.clone().into_bytes();
    let mid = bytes.len() / 2;
    bytes[mid] = if bytes[mid] == b'A' { b'B' } else { b'A' };
    let broken = run(&String::from_utf8(bytes).unwrap(), &pk);
    assert!(matches!(&broken, Decision::Deny(r) if r == "bad_signature" || r == "malformed_token" || r == "invalid_mandate" || r == "unsupported_schema"), "{broken:?}");
    // a token of some other schema under the same key
    let foreign = Biscuit::builder().code("right(\"file1\", \"read\");").unwrap().build(&root()).unwrap().to_base64().unwrap();
    assert_eq!(run(&foreign, &pk), deny("unsupported_schema"));
    // revocation is consulted before the signature
    let mut list = RevocationList::new();
    list.add(&revocation_ids(&m)[0], T0, &root()).unwrap();
    assert_eq!(check_encoded(&token, &other, &ok, &usage(0), &list, &SpentSet::default(), T0, 60), deny("revoked"));
}

#[test]
fn confirmation_rules() {
    let mut d = base_draft();
    d.actions = s(&["files.read", "files.write", "mail.draft", "pay.spend"]);
    d.routine = s(&["mail.draft"]);
    d.confirm = Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: 500 };
    let m = issue(&d, &root()).unwrap();
    let folder = res("folder", "/docs/work/x");
    let host = res("host", "api.example.com");
    assert_eq!(decide(&m, &request("files.read", folder.clone()), T0), Decision::Allow);
    assert_eq!(decide(&m, &request("mail.draft", folder.clone()), T0), Decision::Allow, "routine");
    assert_eq!(decide(&m, &request("files.write", folder.clone()), T0), confirm("each_write"));
    let pay = |v: i64| Request { amounts: spend(v), ..request("pay.spend", host.clone()) };
    assert_eq!(decide(&m, &pay(500), T0), Decision::Allow);
    assert_eq!(decide(&m, &pay(501), T0), confirm("over_amount"));

    // never: only for reads and routine actions
    d.confirm = Confirm::Never;
    assert_eq!(issue(&d, &root()).unwrap_err().code, "invalid_confirm");
    d.actions = s(&["files.read", "mail.draft"]);
    d.limits.clear();
    let quiet = issue(&d, &root()).unwrap();
    assert_eq!(decide(&quiet, &request("mail.draft", folder.clone()), T0), Decision::Allow);

    // a stricter rule added by narrowing
    let strict = narrow(&m, &Narrowing { confirm: Some(Confirm::EachWrite), ..Default::default() }).unwrap();
    assert_eq!(decide(&strict, &pay(1), T0), confirm("each_write"));
    assert_eq!(decide(&strict, &request("mail.draft", folder.clone()), T0), Decision::Allow, "routine stays routine");
    let lower = narrow(&m, &Narrowing { confirm: Some(Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: 100 }), ..Default::default() }).unwrap();
    assert_eq!(decide(&lower, &pay(100), T0), Decision::Allow);
    assert_eq!(decide(&lower, &pay(101), T0), confirm("over_amount"));
    assert_eq!(narrow(&m, &Narrowing { confirm: Some(Confirm::Never), ..Default::default() }).unwrap_err().code, "invalid_narrowing");
}

#[test]
fn approval_token_is_single_use() {
    let m = issue(&base_draft(), &root()).unwrap();
    let host = res("host", "api.example.com");
    let pay = Request { amounts: spend(120), ..request("pay.spend", host.clone()) };
    assert_eq!(decide(&m, &pay, T0), confirm("each_write"));

    // The approving device narrows to this one payment, for five minutes.
    let approval = Narrowing {
        actions: s(&["pay.spend"]),
        resources: vec![host.clone()],
        caps: vec![Cap { kind: "spend".into(), unit: "TWD".into(), amount: 120 }],
        not_after: Some(T0 + 300),
        card: Some("card-3".into()),
        ..Default::default()
    };
    let token = narrow(&m, &approval).unwrap();
    assert_eq!(decide(&token, &pay, T0), deny("wrong_card"));
    let approved = Request { card: Some("card-3".into()), ..pay.clone() };
    let mut spent = SpentSet::default();
    let none = RevocationList::new();
    assert_eq!(check_and_record(&token, &approved, &usage(0), &none, &mut spent, T0 + 10, 60), Decision::Allow);
    assert_eq!(spent.len(), 1);
    assert_eq!(check_and_record(&token, &approved, &usage(120), &none, &mut spent, T0 + 11, 60), deny("already_used"));
    assert_eq!(check(&token, &approved, &usage(120), &none, &spent, T0 + 11), deny("already_used"));
    assert_eq!(check(&token, &Request { amounts: spend(121), ..approved.clone() }, &usage(0), &none, &SpentSet::default(), T0), deny("over_cap"));
    assert_eq!(check(&token, &approved, &usage(0), &none, &SpentSet::default(), T0 + 361), deny("expired"));
    // the entry is kept until the token has expired (plus the tolerance), then dropped
    assert!(spent.contains(&spent_key(&m.id, "card", "card-3"), T0 + 360));
    assert!(!spent.contains(&spent_key(&m.id, "card", "card-3"), T0 + 361));
    spent.purge(T0 + 361);
    assert!(spent.is_empty());

    // a full spent set refuses instead of forgetting
    let mut tiny = SpentSet::new(1);
    tiny.insert("someone/card/else", T0 + 1000, T0).unwrap();
    assert_eq!(check_and_record(&token, &approved, &usage(0), &none, &mut tiny, T0, 60), deny("spent_set_full"));
    assert_eq!(tiny.insert("another", T0 + 5, T0).unwrap_err().code, "spent_set_full");
    assert!(tiny.insert("another", T0 + 2000, T0 + 1001).is_ok(), "expired entries make room");
}

#[test]
fn narrow_refuses_what_is_not_narrower() {
    let m = issue(&base_draft(), &root()).unwrap();
    let cases: Vec<(Narrowing, &str)> = vec![
        (Narrowing::default(), "empty_narrowing"),
        (Narrowing { actions: s(&["mail.send"]), ..Default::default() }, "not_narrower"),
        (Narrowing { resources: vec![res("folder", "/docs")], ..Default::default() }, "not_narrower"),
        (Narrowing { resources: vec![res("folder", "/docs/workshop")], ..Default::default() }, "not_narrower"),
        (Narrowing { resources: vec![res("folder", "*")], ..Default::default() }, "not_narrower"),
        (Narrowing { resources: vec![res("calendar", "x")], ..Default::default() }, "not_narrower"),
        (Narrowing { limits: vec![NarrowLimit { kind: "spend".into(), unit: "TWD".into(), period: "day".into(), amount: 2001 }], ..Default::default() }, "not_narrower"),
        (Narrowing { limits: vec![NarrowLimit { kind: "spend".into(), unit: "USD".into(), period: "day".into(), amount: 1 }], ..Default::default() }, "invalid_narrowing"),
        (Narrowing { not_before: Some(T0 - 1), ..Default::default() }, "not_narrower"),
        (Narrowing { not_after: Some(T0 + 18 * DAY), ..Default::default() }, "not_narrower"),
        (Narrowing { not_before: Some(T0 + 10), not_after: Some(T0 + 5), ..Default::default() }, "not_narrower"),
        (Narrowing { privacy: Some(Privacy::Standard), ..Default::default() }, "not_narrower"),
        (Narrowing { card: Some("c".into()), ..Default::default() }, "invalid_narrowing"),
        (Narrowing { job: Some("has \"quote\"".into()), ..Default::default() }, "invalid_narrowing"),
        (Narrowing { resources: vec![res("folder", "/docs/work/a\"b")], ..Default::default() }, "invalid_narrowing"),
    ];
    for (n, code) in cases {
        assert_eq!(narrow(&m, &n).unwrap_err().code, code, "{n:?}");
    }
    let job = narrow(&m, &Narrowing { job: Some("a".into()), ..Default::default() }).unwrap();
    assert_eq!(narrow(&job, &Narrowing { job: Some("b".into()), ..Default::default() }).unwrap_err().code, "not_narrower");
    assert!(narrow(&job, &Narrowing { job: Some("a".into()), ..Default::default() }).is_ok());
    // a new period for a unit the authority limits is a restriction, so it is allowed
    let month = narrow(&m, &Narrowing { limits: vec![NarrowLimit { kind: "spend".into(), unit: "TWD".into(), period: "month".into(), amount: 5000 }], ..Default::default() }).unwrap();
    let pay = Request { amounts: spend(1), ..request("pay.spend", res("host", "api.example.com")) };
    assert_eq!(decide(&month, &pay, T0), deny("usage_missing"), "the new limit needs its own figure");
}

#[test]
fn draft_rules() {
    let bad = |edit: &dyn Fn(&mut Draft), code: &str| {
        let mut d = base_draft();
        edit(&mut d);
        assert_eq!(propose_from_fields(d).unwrap_err().code, code);
    };
    bad(&|d| d.issuer = "ed25519/00".into(), "invalid_issuer");
    bad(&|d| d.holder = "robot:x".into(), "invalid_holder");
    bad(&|d| d.actions = vec![], "invalid_actions");
    bad(&|d| d.actions = s(&["Files.Read"]), "invalid_actions");
    bad(&|d| d.actions = s(&["files.read", "files.read"]), "invalid_actions");
    bad(&|d| d.routine = s(&["mail.send"]), "invalid_routine");
    bad(&|d| d.resources = vec![], "invalid_resources");
    bad(&|d| d.resources = vec![res("any", "*")], "invalid_resources");
    bad(&|d| d.resources = vec![res("folder", "/")], "invalid_resources");
    bad(&|d| d.resources = vec![res("folder", "/a/")], "invalid_resources");
    bad(&|d| d.resources = vec![res("folder", "relative")], "invalid_resources");
    bad(&|d| d.resources = vec![res("folder", "/a/../b")], "invalid_resources");
    bad(&|d| d.resources = vec![res("folder", "/a\"b")], "invalid_resources");
    bad(&|d| d.resources = vec![res("host", "Example.com")], "invalid_resources");
    bad(&|d| d.limits[0].amount = -1, "invalid_limits");
    bad(&|d| d.limits[0].period = "week".into(), "invalid_limits");
    bad(&|d| d.limits[0].unit = "".into(), "invalid_limits");
    bad(&|d| d.limits.push(Limit { kind: "spend".into(), unit: "TWD".into(), decimals: 0, period: "month".into(), amount: 1 }), "invalid_limits");
    bad(&|d| d.purpose_text = "say \"hi\"".into(), "invalid_purpose");
    bad(&|d| d.purpose_text = "line\nbreak".into(), "invalid_purpose");
    bad(&|d| d.not_after = d.not_before, "invalid_validity");
    bad(&|d| d.not_after = MAX_TIME + 1, "invalid_validity");
    bad(&|d| d.confirm = Confirm::OverAmount { unit: "USD".into(), decimals: 2, amount: 1 }, "invalid_confirm");
    bad(&|d| d.confirm = Confirm::Never, "invalid_confirm");
    bad(&|d| d.period_zone = "Asia/Taipei; drop".into(), "invalid_zone");
    // canonical order
    let mut d = base_draft();
    d.actions.reverse();
    d.resources.reverse();
    d.limits.reverse();
    assert_eq!(propose_from_fields(d).unwrap(), propose_from_fields(base_draft()).unwrap());
    // a draft for someone else's root key is not signed
    let mut d = base_draft();
    d.issuer = format!("ed25519/{}", "ab".repeat(32));
    assert_eq!(issue(&d, &root()).unwrap_err().code, "wrong_issuer");
}

// ---------------------------------------------------------------- widening is impossible

/// Append a block the schema does not know, straight through the library.
fn append_raw(m: &Mandate, code: &str) -> String {
    let biscuit = Biscuit::from_base64(m.to_base64().unwrap(), root().public()).unwrap();
    let block = BlockBuilder::new().code_with_params(code, HashMap::new(), HashMap::new()).unwrap();
    biscuit.append(block).unwrap().to_base64().unwrap()
}

#[test]
fn a_widening_block_changes_nothing() {
    let m = issue(&base_draft(), &root()).unwrap();
    let narrowed = narrow(&m, &Narrowing { actions: s(&["files.read"]), resources: vec![res("folder", "/docs/work/sub")], ..Default::default() }).unwrap();
    let pk = m.draft.issuer.clone();
    let wants = [
        request("mail.send", res("folder", "/docs/work/sub")),      // an action nobody granted
        request("files.read", res("folder", "/etc")),               // a resource nobody granted
        request("files.write", res("folder", "/docs/work/sub/a")),  // an action the narrowing dropped
        request("files.read", res("folder", "/docs/work/other")),   // a resource the narrowing dropped
    ];
    let attacks = [
        "action(\"mail.send\"); action(\"files.write\"); resource(\"folder\", \"*\"); resource(\"folder\", \"/etc\");",
        "action($a) <- req_action($a); resource($k, $v) <- req_resource($k, $v);",
        "check if true;",
        "holder(\"helper:vm\"); valid_until(9999-01-01T00:00:00Z); privacy_max(3); mandate(\"m1_x\"); schema(\"muse.permission\", 1);",
        "req_action(\"files.read\"); req_resource(\"folder\", \"/docs/work/sub\");",
        "allow if true;",
    ];
    for (i, attack) in attacks.iter().enumerate() {
        // "allow if" is not valid in a block at all; the library refuses to build it.
        let Ok(block) = BlockBuilder::new().code_with_params(attack, HashMap::new(), HashMap::new()) else {
            assert_eq!(i, 5);
            continue;
        };
        drop(block);
        let token = append_raw(&narrowed, attack);
        for want in &wants {
            // 1. Our verifier refuses the token outright: the block is not one the schema writes.
            let d = check_encoded(&token, &pk, want, &usage(0), &RevocationList::new(), &SpentSet::default(), T0, 60);
            assert_eq!(d, deny("unknown_block"), "attack {i}");
        }
    }
    // 2. Independently of that rule, the library alone still refuses: facts in a later block
    //    are invisible to the authorizer and to earlier blocks, and a check can only fail.
    //    (Shown on the library's own authorizer, with the schema's policy and request facts.)
    for attack in &attacks[..5] {
        let token = append_raw(&narrowed, attack);
        let biscuit = Biscuit::from_base64(&token, root().public()).unwrap();
        for want in &wants {
            let facts = format!(
                "time(2026-11-13T00:00:00Z); time_late(2026-11-13T00:00:00Z); req_holder(\"helper:vm\"); req_privacy(1); req_action(\"{}\"); req_resource(\"folder\", \"{}\");",
                want.action, want.resource.value
            );
            let result = biscuit_auth::AuthorizerBuilder::new().code(&facts).unwrap().code(AUTHORIZER_POLICY).unwrap().build(&biscuit).unwrap().authorize();
            assert!(result.is_err(), "the library allowed {want:?} after {attack}");
        }
        // and the request the narrowed token does allow is still allowed by the library
        let facts = "time(2026-11-13T00:00:00Z); time_late(2026-11-13T00:00:00Z); req_holder(\"helper:vm\"); req_privacy(1); req_action(\"files.read\"); req_resource(\"folder\", \"/docs/work/sub/a\");";
        let result = biscuit_auth::AuthorizerBuilder::new().code(facts).unwrap().code(AUTHORIZER_POLICY).unwrap().build(&biscuit).unwrap().authorize();
        assert!(result.is_ok(), "{attack}: {result:?}");
    }
}

#[test]
fn third_party_and_sealed_tokens() {
    let m = issue(&base_draft(), &root()).unwrap();
    let pk = m.draft.issuer.clone();
    let biscuit = Biscuit::from_base64(m.to_base64().unwrap(), root().public()).unwrap();
    // a block signed by another key
    let other = KeyPair::new();
    let req = biscuit.third_party_request().unwrap();
    let block = req.create_block(&other.private(), BlockBuilder::new().code("check if req_job(\"j\");").unwrap()).unwrap();
    let token = biscuit.append_third_party(other.public(), block).unwrap().to_base64().unwrap();
    assert_eq!(Mandate::parse(&token, &pk).unwrap_err().code, "unknown_block");
    // a sealed token still verifies and cannot be narrowed
    let sealed = Mandate::parse(&biscuit.seal().unwrap().to_base64().unwrap(), &pk).unwrap();
    assert_eq!(decide(&sealed, &request("files.read", res("folder", "/docs/work")), T0), Decision::Allow);
    let e = narrow(&sealed, &Narrowing { job: Some("j".into()), ..Default::default() }).unwrap_err();
    assert_eq!(e.code, "sealed_token", "{e}");
}

// ---------------------------------------------------------------- the sentence

#[test]
fn the_sentence_says_everything_that_widens() {
    let mut d = base_draft();
    d.actions = s(&["calendar.write", "files.read", "pay.spend", "x.custom"]);
    d.routine = s(&["calendar.write"]);
    d.resources = vec![res("calendar", "*"), res("folder", "/文件/工作"), res("host", "api.example.com"), res("record_kind", "medication")];
    d.limits.push(Limit { kind: "messages".into(), unit: "".into(), decimals: 0, period: "month".into(), amount: 20 });
    d.confirm = Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: 550 };
    d.purpose_code = "TREAT".into();
    let m = issue(&d, &root()).unwrap();
    let en = describe(&m, Lang::En);
    assert_eq!(
        en.sentence,
        "Your helper \"vm\" may add and change calendar events, read files, spend money and do \"x.custom\", limited to any calendar, the folder /文件/工作, the service host api.example.com and records of the kind medication."
    );
    assert_eq!(
        en.lines,
        vec![
            "Up to 20 pushed messages a month",
            "Up to 200 requests a day",
            "Up to NT$20 a day",
            "Days and months are counted in Asia/Taipei time",
            "From 2026-11-13 00:00:00 UTC until 2026-11-30 00:00:00 UTC",
            "Asks you before any payment over NT$5.50, and before every other change",
            "Without asking each time: add and change calendar events",
            "May send data only to services rated private",
            "Purpose: Weekly digest 每週摘要 (TREAT)",
        ]
    );
    let zh = describe(&m, Lang::ZhTw);
    assert_eq!(zh.sentence, "你的助手「vm」可以新增與修改行事曆、讀取檔案、付款、執行「x.custom」，範圍限於任何行事曆、資料夾 /文件/工作、服務主機 api.example.com、「medication」類紀錄。");
    assert_eq!(
        zh.lines,
        vec![
            "每月最多 20 則推送訊息",
            "每日最多 200 次請求",
            "每日最多 NT$20",
            "每日與每月以 Asia/Taipei 時間計算",
            "自 2026-11-13 00:00:00 UTC 起，至 2026-11-30 00:00:00 UTC 止",
            "超過 NT$5.50 的付款，以及其他每次變更，都會先問你",
            "不必每次詢問：新增與修改行事曆",
            "資料只能送往「私密」等級的服務",
            "用途：Weekly digest 每週摘要（TREAT）",
        ]
    );

    // narrowed: the effective permission
    let n = Narrowing {
        actions: s(&["files.read", "pay.spend"]),
        resources: vec![res("folder", "/文件/工作/報告"), res("host", "api.example.com")],
        limits: vec![NarrowLimit { kind: "spend".into(), unit: "TWD".into(), period: "day".into(), amount: 1000 }],
        caps: vec![Cap { kind: "spend".into(), unit: "TWD".into(), amount: 12000 }],
        not_after: Some(T0 + 300),
        privacy: Some(Privacy::Local),
        confirm: Some(Confirm::EachWrite),
        job: Some("job-7".into()),
        card: Some("card-3".into()),
        ..Default::default()
    };
    let en = describe(&narrow(&m, &n).unwrap(), Lang::En);
    assert_eq!(en.sentence, "Your helper \"vm\" may read files and spend money, limited to the folder /文件/工作/報告 and the service host api.example.com.");
    assert_eq!(
        en.lines,
        vec![
            "Up to 20 pushed messages a month",
            "Up to 200 requests a day",
            "Up to NT$10 a day",
            "Days and months are counted in Asia/Taipei time",
            "No more than NT$120 at a time",
            "From 2026-11-13 00:00:00 UTC until 2026-11-13 00:05:00 UTC",
            "Asks you before every change or payment",
            "Nothing may leave your home: local models only",
            "Purpose: Weekly digest 每週摘要 (TREAT)",
            "Only for the job job-7",
            "Only for one approval (card-3)",
        ]
    );
    assert_eq!(format_amount("spend", "USDC", 6, 1_500_000, Lang::En), "1.50 USDC");
    assert_eq!(format_amount("spend", "USDC", 6, 1_234_567, Lang::En), "1.234567 USDC");
    assert_eq!(format_amount("spend", "USD", 2, 5, Lang::En), "US$0.05");
    assert_eq!(format_amount("spend", "JPY", 0, 300, Lang::ZhTw), "JP¥300");
}

// ---------------------------------------------------------------- revocation list

#[test]
fn revocation_list_is_signed_and_merges_by_union() {
    let m = issue(&base_draft(), &root()).unwrap();
    let pk = m.draft.issuer.clone();
    let id = revocation_ids(&m)[0].clone();
    let other_id = "ab".repeat(64);
    let mut a = RevocationList::new();
    a.add(&id, T0 + 50, &root()).unwrap();
    let mut b = RevocationList::new();
    b.add(&id, T0 + 20, &root()).unwrap();
    b.add(&other_id, T0 + 30, &root()).unwrap();
    assert!(a.verify(&pk).is_ok());

    let (mut ab, mut ba) = (a.clone(), b.clone());
    ab.merge(&b);
    ba.merge(&a);
    assert_eq!(ab, ba, "merge is commutative");
    assert_eq!(ab.len(), 2);
    assert!(ab.entries()[0].id < ab.entries()[1].id, "sorted by identifier");
    let kept = ab.entries().into_iter().find(|e| e.id == id).unwrap();
    assert_eq!(kept.at, T0 + 20, "the earlier time wins");
    assert_eq!(kept.signer, pk);
    let mut again = ab.clone();
    again.merge(&ab);
    again.merge(&a);
    assert_eq!(again, ab, "and idempotent");

    // the stored form round-trips and every entry is verified on load
    let text = ab.to_json();
    assert_eq!(RevocationList::from_json(&text, &pk).unwrap(), ab);
    let stranger = parse_private_key(&TEST_ROOT.replace("01", "07")).unwrap();
    assert_eq!(RevocationList::from_json(&text, &public_key_text(&stranger)).unwrap_err().code, "invalid_revocation");
    let mut forged = RevocationList::new();
    forged.add(&id, T0, &stranger).unwrap();
    let forged_entry = forged.entries()[0].token.clone();
    assert_eq!(RevocationList::new().add_entry(&forged_entry, &pk).unwrap_err().code, "invalid_revocation");
    assert_eq!(RevocationList::new().add_entry(&m.to_base64().unwrap(), &pk).unwrap_err().code, "invalid_revocation", "a permission is not an entry");
    assert_eq!(RevocationList::new().add("short", T0, &root()).unwrap_err().code, "invalid_revocation");
    assert_eq!(RevocationList::from_json("{\"v\": 2, \"entries\": []}", &pk).unwrap_err().code, "invalid_revocation");
}

// ---------------------------------------------------------------- properties

struct Rng(u64);

impl Rng {
    fn next(&mut self) -> u64 {
        self.0 ^= self.0 << 13;
        self.0 ^= self.0 >> 7;
        self.0 ^= self.0 << 17;
        self.0
    }

    fn below(&mut self, n: u64) -> u64 {
        self.next() % n
    }

    fn pick<'a, T>(&mut self, items: &'a [T]) -> &'a T {
        &items[self.below(items.len() as u64) as usize]
    }

    fn subset<T: Clone>(&mut self, items: &[T]) -> Vec<T> {
        let mut out: Vec<T> = items.iter().filter(|_| self.below(2) == 0).cloned().collect();
        if out.is_empty() {
            out.push(self.pick(items).clone());
        }
        out
    }
}

const ACTIONS: [&str; 7] = ["files.read", "files.write", "mail.draft", "pay.spend", "llm.call", "llm.paid", "x.custom"];
const FOLDERS: [&str; 7] = ["/docs", "/docs/work", "/docs/work/sub", "/docs/workshop", "/文件", "/文件/工作", "/a b"];
const HOSTS: [&str; 3] = ["api.example.com", "b.example.com", "c.example.com:8443"];
const PRIVACY: [Privacy; 4] = [Privacy::Local, Privacy::Private, Privacy::Standard, Privacy::Public];

fn random_resources(rng: &mut Rng) -> Vec<Resource> {
    let mut out: Vec<Resource> = rng.subset(&FOLDERS).iter().map(|f| res("folder", f)).collect();
    out.extend(rng.subset(&HOSTS).iter().map(|h| res("host", h)));
    match rng.below(6) {
        0 => out.push(res("folder", "*")),
        1 => out.push(res("host", "*")),
        2 => out.push(res("calendar", "work")),
        _ => {}
    }
    out
}

fn random_draft(rng: &mut Rng) -> Draft {
    let mut d = base_draft();
    d.actions = rng.subset(&ACTIONS).iter().map(|a| a.to_string()).collect();
    d.routine = d.actions.iter().filter(|_| rng.below(4) == 0).cloned().collect();
    d.resources = random_resources(rng);
    d.limits = vec![
        Limit { kind: "spend".into(), unit: "TWD".into(), decimals: 2, period: "day".into(), amount: 100 + rng.below(2000) as i64 },
        Limit { kind: "requests".into(), unit: "".into(), decimals: 0, period: "day".into(), amount: 1 + rng.below(50) as i64 },
    ];
    if rng.below(2) == 0 {
        d.limits.push(Limit { kind: "spend".into(), unit: "TWD".into(), decimals: 2, period: "month".into(), amount: 3000 + rng.below(5000) as i64 });
    }
    d.not_before = T0 + rng.below(1000) as i64;
    d.not_after = d.not_before + 1 + rng.below(3 * DAY as u64) as i64;
    d.privacy = *rng.pick(&PRIVACY);
    d.confirm = match rng.below(3) {
        0 => Confirm::EachWrite,
        1 => Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: rng.below(600) as i64 },
        _ if d.actions.iter().all(|a| action_class(a) == "read" || d.routine.contains(a)) => Confirm::Never,
        _ => Confirm::EachWrite,
    };
    propose_from_fields(d).unwrap()
}

/// A narrowing that `narrow` accepts for `m`, or None if this draw found nothing to restrict.
fn random_narrowing(rng: &mut Rng, m: &Mandate) -> Option<Narrowing> {
    let mut n = Narrowing::default();
    if rng.below(2) == 0 && !m.actions().is_empty() {
        n.actions = rng.subset(&m.actions());
    }
    if rng.below(2) == 0 {
        // candidates: everything any level could mean, kept only if the token covers it
        let mut candidates: Vec<Resource> = FOLDERS.iter().map(|f| res("folder", f)).chain(HOSTS.iter().map(|h| res("host", h))).collect();
        candidates.extend([res("folder", "/docs/work/sub/deep"), res("folder", "*"), res("host", "*"), res("calendar", "work")]);
        let effective = m.resources();
        candidates.retain(|c| effective.iter().any(|e| e.covers(c)));
        if !candidates.is_empty() {
            n.resources = rng.subset(&candidates);
            n.resources.truncate(4);
        }
    }
    if rng.below(3) == 0 {
        let l = rng.pick(&m.limits()).clone();
        n.limits.push(NarrowLimit { kind: l.kind, unit: l.unit, period: l.period, amount: rng.below(l.amount as u64 + 1) as i64 });
    }
    if rng.below(3) == 0 {
        let old = m.caps().iter().find(|c| c.kind == "spend").map_or(5000, |c| c.amount);
        n.caps.push(Cap { kind: "spend".into(), unit: "TWD".into(), amount: rng.below(old as u64 + 1) as i64 });
    }
    let (from, until) = (m.not_before(), m.not_after());
    if rng.below(3) == 0 {
        n.not_before = Some(from + rng.below((until - from) as u64 + 1) as i64);
    }
    if rng.below(3) == 0 {
        let start = n.not_before.unwrap_or(from);
        n.not_after = Some(start + rng.below((until - start) as u64 + 1) as i64);
    }
    if rng.below(3) == 0 {
        n.privacy = Some(PRIVACY[rng.below(m.privacy().rank() as u64 + 1) as usize]);
    }
    match rng.below(6) {
        0 => n.confirm = Some(Confirm::EachWrite),
        1 => n.confirm = Some(Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: rng.below(600) as i64 }),
        _ => {}
    }
    if rng.below(4) == 0 {
        n.job = Some(m.jobs().first().cloned().unwrap_or_else(|| format!("job-{}", rng.below(3))));
    }
    if rng.below(8) == 0 {
        n.tool = Some(m.tools().first().cloned().unwrap_or_else(|| format!("call-{}", rng.below(3))));
        n.not_after = n.not_after.or(Some(until));
    }
    if rng.below(8) == 0 {
        n.card = Some(m.cards().first().cloned().unwrap_or_else(|| format!("card-{}", rng.below(3))));
        n.not_after = n.not_after.or(Some(until));
    }
    (n != Narrowing::default()).then_some(n)
}

/// Requests around every boundary of `m`: path segments, amounts at each limit and cap
/// plus and minus one, times at each end plus and minus the tolerance, each privacy
/// class, a wrong holder, and right, wrong and missing job, tool and card ids.
fn boundary_requests(rng: &mut Rng, m: &Mandate) -> Vec<(Request, Usage, i64)> {
    let mut folders: Vec<String> = FOLDERS.iter().map(|f| f.to_string()).collect();
    for r in m.resources().iter().chain(&m.draft.resources).filter(|r| r.kind == "folder" && r.value != "*") {
        folders.extend([r.value.clone(), format!("{}/x", r.value), format!("{}x", r.value), format!("{}/x/y", r.value)]);
        if let Some((parent, _)) = r.value.rsplit_once('/').filter(|(p, _)| !p.is_empty()) {
            folders.push(parent.to_string());
        }
    }
    let mut resources: Vec<Resource> = folders.iter().map(|f| res("folder", f)).collect();
    resources.extend(HOSTS.iter().map(|h| res("host", h)));
    resources.extend([res("host", "evil.example.com"), res("calendar", "work"), res("calendar", "other")]);

    let mut times = vec![T0 + 500];
    for edge in [m.not_before(), m.not_after(), m.draft.not_before, m.draft.not_after] {
        times.extend([edge - 61, edge - 60, edge - 1, edge, edge + 1, edge + 60, edge + 61]);
    }
    let mut amounts: Vec<i64> = vec![0, 1];
    for l in m.limits().iter().chain(&m.draft.limits).filter(|l| l.kind == "spend") {
        amounts.extend([l.amount - 1, l.amount, l.amount + 1, l.amount / 2]);
    }
    for c in m.caps() {
        amounts.extend([c.amount - 1, c.amount, c.amount + 1]);
    }
    for rule in m.confirm_rules() {
        if let Confirm::OverAmount { amount, .. } = rule {
            amounts.extend([amount - 1, amount, amount + 1]);
        }
    }
    amounts.retain(|a| *a >= 0);
    let ids = |bound: Vec<String>, prefix: &str| -> Vec<Option<String>> {
        let mut v = vec![None, Some(format!("{prefix}-0")), Some(format!("{prefix}-1")), Some(format!("{prefix}-9"))];
        v.extend(bound.into_iter().map(Some));
        v
    };
    let (jobs, tools, cards) = (ids(m.jobs(), "job"), ids(m.tools(), "call"), ids(m.cards(), "card"));

    let mut out = Vec::new();
    for _ in 0..60 {
        // Mostly aim at what the token allows, so that boundaries are hit from the inside too.
        let aim = rng.below(4) != 0;
        let action = if aim && !m.actions().is_empty() { rng.pick(&m.actions()).clone() } else { rng.pick(&ACTIONS).to_string() };
        let resource = rng.pick(&resources).clone();
        let mut r = request(&action, resource);
        r.holder = if rng.below(12) == 0 { "helper:other".into() } else { "helper:vm".into() };
        r.privacy = if aim { PRIVACY[rng.below(m.privacy().rank() as u64 + 1) as usize] } else { *rng.pick(&PRIVACY) }.name().to_string();
        if action_class(&action) == "spend" || rng.below(5) == 0 {
            r.amounts = spend(*rng.pick(&amounts));
        }
        if rng.below(4) == 0 {
            r.amounts.push(Amount { kind: "requests".into(), unit: "".into(), value: 1 });
        }
        let bound = |list: &Vec<Option<String>>, rng: &mut Rng| if aim { list.last().cloned().flatten().or_else(|| rng.pick(list).clone()) } else { rng.pick(list).clone() };
        r.job = bound(&jobs, rng);
        r.tool = bound(&tools, rng);
        r.card = bound(&cards, rng);
        let mut u = Usage::new();
        for l in m.limits() {
            let used = match rng.below(4) {
                0 => 0,
                1 => l.amount,
                2 => rng.below(l.amount as u64 + 1) as i64,
                _ => (l.amount - r.amounts.iter().find(|a| a.kind == l.kind).map_or(0, |a| a.value)).max(0),
            };
            if rng.below(15) != 0 {
                u.set(&l.kind, &l.unit, &l.period, used);
            }
        }
        out.push((r, u, *rng.pick(&times)));
    }
    out
}

/// How permissive an answer is: deny < ask < allow.
fn level(d: &Decision) -> u8 {
    match d {
        Decision::Deny(_) => 0,
        Decision::NeedsConfirmation(_) => 1,
        Decision::Allow => 2,
    }
}

#[test]
fn a_narrowed_token_never_does_more_than_its_parent() {
    let none = RevocationList::new();
    let spent = SpentSet::default();
    let (mut checks, mut allows, mut asks, mut child_allows, mut narrowings) = (0u32, 0u32, 0u32, 0u32, 0u32);
    for seed in 1..=120u64 {
        let mut rng = Rng(seed.wrapping_mul(0x9E37_79B9_7F4A_7C15) | 1);
        let root_token = issue(&random_draft(&mut rng), &root()).unwrap();
        let mut chain = vec![root_token];
        for _ in 0..(1 + rng.below(4)) {
            let parent = chain.last().unwrap();
            if let Some(n) = random_narrowing(&mut rng, parent) {
                let child = narrow(parent, &n).unwrap_or_else(|e| panic!("seed {seed}: {e} for {n:?}"));
                // what was appended reads back as what was asked for
                let reread = Mandate::parse(&child.to_base64().unwrap(), &child.draft.issuer).unwrap();
                assert_eq!(reread.levels, child.levels, "seed {seed}");
                chain.push(child);
                narrowings += 1;
            }
        }
        let last = chain.last().unwrap().clone();
        for (r, u, now) in boundary_requests(&mut rng, &last) {
            let decisions: Vec<Decision> = chain.iter().map(|m| check(m, &r, &u, &none, &spent, now)).collect();
            checks += decisions.len() as u32;
            for d in &decisions {
                assert_ne!(*d, deny("engine_disagreement"), "seed {seed}: {r:?}");
                allows += (*d == Decision::Allow) as u32;
                asks += (level(d) == 1) as u32;
            }
            child_allows += (chain.len() > 1 && *decisions.last().unwrap() == Decision::Allow) as u32;
            for (i, pair) in decisions.windows(2).enumerate() {
                let (parent, child) = (&pair[0], &pair[1]);
                // A card binding is the one thing that turns "ask" into "allow": it is the
                // person's approval. It never turns a refusal into anything.
                let approved_here = chain[i].cards().is_empty() && !chain[i + 1].cards().is_empty();
                let ok = if approved_here { level(child) == 0 || level(parent) >= 1 } else { level(child) <= level(parent) };
                assert!(ok, "seed {seed}, level {i}: parent {parent:?}, child {child:?}\nrequest {r:?}\nusage {u:?} now {now}\nparent levels {:?}\nchild adds {:?}", chain[i].levels, chain[i + 1].levels.last());
            }
            // the sentence's intersection agrees with checking every level
            let listed = last.actions().contains(&r.action) && last.resources().iter().any(|e| e.covers(&r.resource));
            let per_level = last.draft.actions.contains(&r.action)
                && last.levels.iter().all(|l| l.actions.is_empty() || l.actions.contains(&r.action))
                && last.draft.resources.iter().any(|e| e.covers(&r.resource))
                && last.levels.iter().all(|l| l.resources.is_empty() || l.resources.iter().any(|e| e.covers(&r.resource)));
            assert_eq!(listed, per_level, "seed {seed}: describe would differ from check for {r:?}");
        }
    }
    // the generator must actually exercise both sides of the boundaries
    assert!(narrowings > 200, "{narrowings}");
    assert!(allows > 300 && asks > 100 && child_allows > 50, "allows {allows}, asks {asks}, narrowed allows {child_allows} of {checks}");
}

#[test]
fn every_widening_fact_is_in_the_text() {
    for seed in 1..=60u64 {
        let mut rng = Rng(seed.wrapping_mul(0xD1B5_4A32_D192_ED03) | 1);
        let m = issue(&random_draft(&mut rng), &root()).unwrap();
        for lang in [Lang::En, Lang::ZhTw] {
            let d = describe(&m, lang);
            let all = format!("{}\n{}", d.sentence, d.lines.join("\n"));
            assert!(all.contains("vm"), "holder");
            for a in &m.draft.actions {
                let other = issue(&Draft { actions: vec![a.clone()], routine: vec![], confirm: Confirm::EachWrite, ..m.draft.clone() }, &root()).unwrap();
                let phrase = describe(&other, lang).sentence;
                let phrase = phrase.split(if lang == Lang::En { " may " } else { "可以" }).nth(1).unwrap().split(if lang == Lang::En { ", limited to" } else { "，範圍" }).next().unwrap().to_string();
                assert!(d.sentence.contains(&phrase), "seed {seed}: action {a} ({phrase}) missing from {}", d.sentence);
            }
            for r in &m.draft.resources {
                let needle = if r.value == "*" { if lang == Lang::En { "any ".to_string() } else { "任何".to_string() } } else { r.value.clone() };
                assert!(d.sentence.contains(&needle), "seed {seed}: resource {r:?}");
            }
            for l in &m.draft.limits {
                let amount = format_amount(&l.kind, &l.unit, l.decimals, l.amount, lang);
                assert!(d.lines.iter().any(|line| line.contains(&amount)), "seed {seed}: limit {l:?}");
            }
            assert!(all.contains(&format_time(m.draft.not_after)) && all.contains(&format_time(m.draft.not_before)));
            assert!(all.contains("Asia/Taipei"));
            for a in &m.draft.routine {
                if m.draft.confirm != Confirm::Never {
                    assert!(d.lines.iter().any(|l| l.starts_with("Without asking") || l.starts_with("不必每次詢問")), "seed {seed}: routine {a}");
                }
            }
            if let Confirm::OverAmount { amount, .. } = &m.draft.confirm {
                assert!(all.contains(&format_amount("spend", "TWD", 2, *amount, lang)));
            }
            if m.draft.confirm == Confirm::Never {
                assert!(all.contains("Does not ask") || all.contains("不會先問"));
            }
            // each privacy class has its own line
            let privacy_lines: Vec<String> = PRIVACY.iter().map(|p| describe(&issue(&Draft { privacy: *p, ..m.draft.clone() }, &root()).unwrap(), lang).lines.join("\n")).collect();
            for (i, a) in privacy_lines.iter().enumerate() {
                for b in &privacy_lines[i + 1..] {
                    assert_ne!(a, b);
                }
            }
        }
    }
}
