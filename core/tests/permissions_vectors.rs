//! Replays `conformance/permissions.json` and requires the answers the Python twin gave.
//!
//! Token bytes cannot be compared between the two implementations: every block is signed
//! with a fresh random key. So the vectors carry tokens made once (by this file's ignored
//! `generate_fixture_tokens` test, and a few by the Python twin) with test-only keys, and
//! both sides must read the same tokens the same way: same decision and reason for every
//! request, same sentence, same revocation identifiers. Where a vector narrows or signs,
//! each side does it itself and the description of the result is compared.
//!
//! Regenerate the Rust-made fixtures (only when the schema changes):
//!     cargo test --test permissions_vectors -- --ignored generate_fixture_tokens
//! then `python scripts/export_conformance.py` to recompute the expected answers.

use std::collections::{BTreeMap, HashMap};
use std::fs;
use std::path::Path;

use biscuit_auth::{Biscuit, BlockBuilder};
use muse_core::permissions::*;
use serde_json::{json, Value};

// ---------------------------------------------------------------- JSON <-> values

fn text(v: &Value) -> String {
    v.as_str().unwrap_or_else(|| panic!("not a string: {v}")).to_string()
}

fn int(v: &Value) -> i64 {
    v.as_i64().unwrap_or_else(|| panic!("not an integer: {v}"))
}

fn opt_text(v: Option<&Value>) -> Option<String> {
    v.filter(|x| !x.is_null()).map(text)
}

fn rows(v: Option<&Value>) -> Vec<Vec<Value>> {
    v.and_then(Value::as_array).map(|a| a.iter().map(|r| r.as_array().expect("row").clone()).collect()).unwrap_or_default()
}

fn strings(v: Option<&Value>) -> Vec<String> {
    v.and_then(Value::as_array).map(|a| a.iter().map(text).collect()).unwrap_or_default()
}

fn confirm_of(v: &Value, code: &'static str) -> Result<Confirm, PermissionError> {
    let a = v.as_array().expect("confirm");
    match (a[0].as_str().unwrap(), a.len()) {
        ("each_write", 1) => Ok(Confirm::EachWrite),
        ("never", 1) => Ok(Confirm::Never),
        ("over_amount", 4) => Ok(Confirm::OverAmount { unit: text(&a[1]), decimals: int(&a[2]), amount: int(&a[3]) }),
        _ => Err(PermissionError::new(code, "unknown confirmation rule")),
    }
}

fn confirm_data(c: &Confirm) -> Value {
    match c {
        Confirm::OverAmount { unit, decimals, amount } => json!(["over_amount", unit, decimals, amount]),
        other => json!([other.name()]),
    }
}

/// A draft from its JSON form. A value the Rust types cannot hold (an unknown privacy
/// class or confirmation rule) is the error Python's validation gives for it.
fn draft_of(v: &Value) -> Result<Draft, PermissionError> {
    Ok(Draft {
        issuer: text(&v["issuer"]),
        holder: text(&v["holder"]),
        actions: strings(v.get("actions")),
        routine: strings(v.get("routine")),
        resources: rows(v.get("resources")).iter().map(|r| Resource { kind: text(&r[0]), value: text(&r[1]) }).collect(),
        limits: rows(v.get("limits")).iter().map(|r| Limit { kind: text(&r[0]), unit: text(&r[1]), decimals: int(&r[2]), period: text(&r[3]), amount: int(&r[4]) }).collect(),
        purpose_text: text(&v["purpose_text"]),
        purpose_code: text(&v["purpose_code"]),
        issued_at: int(&v["issued_at"]),
        not_before: int(&v["not_before"]),
        not_after: int(&v["not_after"]),
        confirm: confirm_of(&v["confirm"], "invalid_confirm")?,
        privacy: Privacy::from_name(&text(&v["privacy"])).ok_or_else(|| PermissionError::new("invalid_privacy", "unknown class"))?,
        period_zone: text(&v["period_zone"]),
    })
}

fn draft_data(d: &Draft) -> Value {
    json!({
        "issuer": d.issuer, "holder": d.holder, "actions": d.actions, "routine": d.routine,
        "resources": d.resources.iter().map(|r| json!([r.kind, r.value])).collect::<Vec<_>>(),
        "limits": d.limits.iter().map(|l| json!([l.kind, l.unit, l.decimals, l.period, l.amount])).collect::<Vec<_>>(),
        "purpose_text": d.purpose_text, "purpose_code": d.purpose_code,
        "issued_at": d.issued_at, "not_before": d.not_before, "not_after": d.not_after,
        "confirm": confirm_data(&d.confirm), "privacy": d.privacy.name(), "period_zone": d.period_zone,
    })
}

fn narrowing_of(v: &Value) -> Result<Narrowing, PermissionError> {
    let bad = || PermissionError::new("invalid_narrowing", "a value the types cannot hold");
    Ok(Narrowing {
        actions: strings(v.get("actions")),
        resources: rows(v.get("resources")).iter().map(|r| Resource { kind: text(&r[0]), value: text(&r[1]) }).collect(),
        limits: rows(v.get("limits")).iter().map(|r| NarrowLimit { kind: text(&r[0]), unit: text(&r[1]), period: text(&r[2]), amount: int(&r[3]) }).collect(),
        caps: rows(v.get("caps")).iter().map(|r| Cap { kind: text(&r[0]), unit: text(&r[1]), amount: int(&r[2]) }).collect(),
        not_before: v.get("not_before").filter(|x| !x.is_null()).map(int),
        not_after: v.get("not_after").filter(|x| !x.is_null()).map(int),
        privacy: match opt_text(v.get("privacy")) {
            Some(name) => Some(Privacy::from_name(&name).ok_or_else(bad)?),
            None => None,
        },
        confirm: match v.get("confirm").filter(|x| !x.is_null()) {
            Some(c) => Some(confirm_of(c, "invalid_narrowing")?),
            None => None,
        },
        job: opt_text(v.get("job")),
        tool: opt_text(v.get("tool")),
        card: opt_text(v.get("card")),
    })
}

fn narrowing_data(n: &Narrowing) -> Value {
    let mut out = serde_json::Map::new();
    if !n.actions.is_empty() {
        out.insert("actions".into(), json!(n.actions));
    }
    if !n.resources.is_empty() {
        out.insert("resources".into(), n.resources.iter().map(|r| json!([r.kind, r.value])).collect());
    }
    if !n.limits.is_empty() {
        out.insert("limits".into(), n.limits.iter().map(|l| json!([l.kind, l.unit, l.period, l.amount])).collect());
    }
    if !n.caps.is_empty() {
        out.insert("caps".into(), n.caps.iter().map(|c| json!([c.kind, c.unit, c.amount])).collect());
    }
    for (name, value) in [("not_before", n.not_before), ("not_after", n.not_after)] {
        if let Some(t) = value {
            out.insert(name.into(), json!(t));
        }
    }
    if let Some(p) = n.privacy {
        out.insert("privacy".into(), json!(p.name()));
    }
    if let Some(c) = &n.confirm {
        out.insert("confirm".into(), confirm_data(c));
    }
    for (name, value) in [("job", &n.job), ("tool", &n.tool), ("card", &n.card)] {
        if let Some(id) = value {
            out.insert(name.into(), json!(id));
        }
    }
    Value::Object(out)
}

fn request_of(v: &Value) -> Request {
    let r = v["resource"].as_array().expect("resource");
    Request {
        holder: text(&v["holder"]),
        action: text(&v["action"]),
        resource: Resource { kind: text(&r[0]), value: text(&r[1]) },
        amounts: rows(v.get("amounts")).iter().map(|a| Amount { kind: text(&a[0]), unit: text(&a[1]), value: int(&a[2]) }).collect(),
        privacy: text(&v["privacy"]),
        job: opt_text(v.get("job")),
        tool: opt_text(v.get("tool")),
        card: opt_text(v.get("card")),
    }
}

fn usage_of(v: &Value) -> Usage {
    let mut u = Usage::new();
    for row in rows(Some(v)) {
        u.set(&text(&row[0]), &text(&row[1]), &text(&row[2]), int(&row[3]));
    }
    u
}

fn spent_of(v: &Value) -> SpentSet {
    let mut s = SpentSet::default();
    for row in rows(Some(v)) {
        s.insert(&text(&row[0]), int(&row[1]), 0).unwrap();
    }
    s
}

fn revoked_of(v: &Value, root: &str) -> RevocationList {
    let mut list = RevocationList::new();
    for entry in v.as_array().expect("revoked") {
        list.add_entry(&text(entry), root).expect("vectors carry valid entries here");
    }
    list
}

fn decision_data(d: &Decision) -> Value {
    match d {
        Decision::Allow => json!(["allow", ""]),
        Decision::Deny(reason) => json!(["deny", reason]),
        Decision::NeedsConfirmation(rule) => json!(["needs_confirmation", rule]),
    }
}

fn description_data(m: &Mandate, lang: Lang) -> Value {
    let d = describe(m, lang);
    json!({"sentence": d.sentence, "lines": d.lines})
}

fn mandate_data(m: &Mandate) -> Value {
    json!({
        "id": m.id,
        "blocks": m.levels.len() + 1,
        "revocation_ids": revocation_ids(m),
        "draft": draft_data(&m.draft),
        "levels": m.levels.iter().map(narrowing_data).collect::<Vec<_>>(),
        "effective": {
            "actions": m.actions(),
            "resources": m.resources().iter().map(|r| json!([r.kind, r.value])).collect::<Vec<_>>(),
            "limits": m.limits().iter().map(|l| json!([l.kind, l.unit, l.decimals, l.period, l.amount])).collect::<Vec<_>>(),
            "caps": m.caps().iter().map(|c| json!([c.kind, c.unit, c.amount])).collect::<Vec<_>>(),
            "not_before": m.not_before(), "not_after": m.not_after(), "privacy": m.privacy().name(),
            "jobs": m.jobs(), "tools": m.tools(), "cards": m.cards(),
        },
        "en": description_data(m, Lang::En),
        "zh-TW": description_data(m, Lang::ZhTw),
    })
}

// ---------------------------------------------------------------- running one vector

fn run(function: &str, input: &Value) -> Result<Value, PermissionError> {
    match function {
        "permissions.propose" => {
            let d = propose_from_fields(draft_of(&input["draft"])?)?;
            Ok(json!({"id": mandate_id(&d), "draft": draft_data(&d)}))
        }
        "permissions.read" => Ok(mandate_data(&Mandate::parse(&text(&input["token"]), &text(&input["root"]))?)),
        "permissions.check" => {
            let root = text(&input["root"]);
            let d = check_encoded(
                &text(&input["token"]),
                &root,
                &request_of(&input["request"]),
                &usage_of(&input["usage"]),
                &revoked_of(&input["revoked"], &root),
                &spent_of(&input["spent"]),
                int(&input["now"]),
                int(&input["skew"]),
            );
            Ok(decision_data(&d))
        }
        "permissions.narrow" => {
            let parent = Mandate::parse(&text(&input["token"]), &text(&input["root"]))?;
            let mut child = parent;
            for step in input["narrowings"].as_array().expect("narrowings") {
                child = narrow(&child, &narrowing_of(step)?)?;
            }
            // what was appended must read back the same from the bytes
            let reread = Mandate::parse(&child.to_base64()?, &child.draft.issuer)?;
            assert_eq!(reread.levels, child.levels);
            let mut out = mandate_data(&reread);
            out.as_object_mut().unwrap().remove("revocation_ids"); // random per signing
            Ok(out)
        }
        "permissions.issue" => {
            let root = parse_private_key(&text(&input["root_private"]))?;
            let m = issue(&draft_of(&input["draft"])?, &root)?;
            let mut out = mandate_data(&Mandate::parse(&m.to_base64()?, &public_key_text(&root))?);
            out.as_object_mut().unwrap().remove("revocation_ids");
            Ok(out)
        }
        "permissions.sequence" => {
            // one spent set across several requests: single-use tokens and replay
            let root = text(&input["root"]);
            let mut spent = SpentSet::new(int(&input["capacity"]) as usize);
            let none = RevocationList::new();
            let mut out = Vec::new();
            for step in input["steps"].as_array().expect("steps") {
                let m = Mandate::parse(&text(&step["token"]), &root)?;
                let d = check_and_record(&m, &request_of(&step["request"]), &usage_of(&step["usage"]), &none, &mut spent, int(&step["now"]), int(&input["skew"]));
                out.push(decision_data(&d));
            }
            Ok(json!({"decisions": out, "spent": spent.entries().iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>()}))
        }
        "permissions.revocations" => {
            let root = text(&input["root"]);
            let mut merged = RevocationList::new();
            for list in input["lists"].as_array().expect("lists") {
                let mut one = RevocationList::new();
                for entry in list.as_array().expect("list") {
                    one.add_entry(&text(entry), &root)?;
                }
                merged.merge(&one);
            }
            merged.verify(&root)?;
            let again = RevocationList::from_json(&merged.to_json(), &root)?;
            assert_eq!(again, merged);
            Ok(json!({
                "entries": merged.entries().iter().map(|e| json!([e.id, e.at, e.signer])).collect::<Vec<_>>(),
                "json": merged.to_json(),
                "contains": input["ask"].as_array().expect("ask").iter().map(|id| merged.contains(&text(id))).collect::<Vec<_>>(),
            }))
        }
        "permissions.revoke" => {
            // each side signs its own entry; the entry's content is compared, not its bytes
            let root = parse_private_key(&text(&input["root_private"]))?;
            let mut list = RevocationList::new();
            list.add(&text(&input["id"]), int(&input["at"]), &root)?;
            let e = RevocationEntry::verify(&list.entries()[0].token, &public_key_text(&root))?;
            Ok(json!([e.id, e.at, e.signer]))
        }
        "permissions.spent" => {
            let mut set = SpentSet::new(int(&input["capacity"]) as usize);
            let mut out = Vec::new();
            for op in input["ops"].as_array().expect("ops") {
                let a = op.as_array().unwrap();
                out.push(match a[0].as_str().unwrap() {
                    "insert" => match set.insert(&text(&a[1]), int(&a[2]), int(&a[3])) {
                        Ok(()) => json!("ok"),
                        Err(e) => json!(e.code),
                    },
                    "contains" => json!(set.contains(&text(&a[1]), int(&a[2]))),
                    "room" => json!(set.has_room(int(&a[1]) as usize, int(&a[2]))),
                    "purge" => {
                        set.purge(int(&a[1]));
                        json!(set.len())
                    }
                    other => panic!("unknown op {other}"),
                });
            }
            Ok(json!({"results": out, "entries": set.entries().iter().map(|(k, v)| json!([k, v])).collect::<Vec<_>>()}))
        }
        "permissions.format" => {
            let lang = |tag: &str| Lang::from_tag(tag).expect("lang");
            Ok(match input["what"].as_str().unwrap() {
                "amount" => json!(format_amount(&text(&input["kind"]), &text(&input["unit"]), int(&input["decimals"]), int(&input["amount"]), lang(&text(&input["lang"])))),
                "time" => json!([format_time(int(&input["t"])), rfc3339(int(&input["t"]))]),
                "class" => json!(action_class(&text(&input["action"]))),
                other => panic!("unknown format {other}"),
            })
        }
        other => panic!("unknown function {other}"),
    }
}

#[test]
fn every_permission_vector_matches_python() {
    let path = Path::new(env!("CARGO_MANIFEST_DIR")).join("conformance/permissions.json");
    let vectors: Vec<Value> = serde_json::from_str(&fs::read_to_string(&path).expect("conformance/permissions.json")).unwrap();
    assert!(vectors.len() > 300, "only {} vectors", vectors.len());
    let mut counts: BTreeMap<String, usize> = BTreeMap::new();
    let mut failures = Vec::new();
    for v in &vectors {
        let function = v["fn"].as_str().unwrap();
        *counts.entry(function.to_string()).or_default() += 1;
        let got = match run(function, &v["input"]) {
            Ok(value) => json!({"ok": value}),
            Err(e) => json!({"err": e.code}),
        };
        if got != v["expected"] {
            failures.push(format!("{}\n   python: {}\n   rust:   {}", v["name"], v["expected"], got));
        }
    }
    assert!(failures.is_empty(), "{} of {} vectors differ:\n{}", failures.len(), vectors.len(), failures.join("\n"));
    println!("permissions vectors: {counts:?}");
}

// ---------------------------------------------------------------- making the fixture tokens

const T0: i64 = 1_794_528_000; // 2026-11-13T00:00:00Z
const DAY: i64 = 86_400;
/// Test-only keys. They sign nothing but these fixtures.
const ROOT: &str = "ed25519-private/0101010101010101010101010101010101010101010101010101010101010101";
const OTHER: &str = "ed25519-private/0202020202020202020202020202020202020202020202020202020202020202";

fn s(items: &[&str]) -> Vec<String> {
    items.iter().map(|x| x.to_string()).collect()
}

fn base_draft(root: &KeyPair) -> Draft {
    Draft {
        issuer: public_key_text(root),
        holder: "helper:vm".into(),
        actions: s(&["files.read", "files.write", "pay.spend"]),
        routine: vec![],
        resources: vec![Resource::new("folder", "/docs/work"), Resource::new("host", "api.example.com")],
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

/// Append a block the schema's own `narrow` would not write, straight through the library.
fn raw_block(token: &str, root: &KeyPair, code: &str, params: &[(&str, &str)]) -> String {
    let biscuit = Biscuit::from_base64(token, root.public()).unwrap();
    let params: HashMap<String, biscuit_auth::builder::Term> = params.iter().map(|(k, v)| (k.to_string(), biscuit_auth::builder::Term::Str(v.to_string()))).collect();
    let block = BlockBuilder::new().code_with_params(code, params, HashMap::new()).unwrap();
    biscuit.append(block).unwrap().to_base64().unwrap()
}

/// An authority block written by hand (to make tokens `issue` refuses to make).
fn raw_authority(root: &KeyPair, text: &str) -> String {
    Biscuit::builder().code(text).unwrap().build(root).unwrap().to_base64().unwrap()
}

#[test]
#[ignore = "writes conformance/fixtures/permission_tokens.json; run only when the schema changes"]
fn generate_fixture_tokens() {
    let root = parse_private_key(ROOT).unwrap();
    let other = parse_private_key(OTHER).unwrap();
    let mut tokens: BTreeMap<String, String> = BTreeMap::new();
    let mut put = |name: &str, m: &Mandate| {
        tokens.insert(name.to_string(), m.to_base64().unwrap());
    };

    // ---- issued
    let base = issue(&base_draft(&root), &root).unwrap();
    put("base", &base);
    let mut d = base_draft(&root);
    d.actions = s(&["calendar.write", "files.read", "files.write", "mail.draft", "pay.spend", "x.custom"]);
    d.routine = s(&["calendar.write", "mail.draft"]);
    d.resources = vec![
        Resource::new("calendar", "*"),
        Resource::new("folder", "/文件/工作"),
        Resource::new("folder", "/Users/me/My Documents"),
        Resource::new("host", "api.example.com"),
        Resource::new("host", "pay.example.com:8443"),
        Resource::new("record_kind", "medication"),
    ];
    d.limits.push(Limit { kind: "messages".into(), unit: "".into(), decimals: 0, period: "month".into(), amount: 20 });
    d.limits.push(Limit { kind: "spend".into(), unit: "TWD".into(), decimals: 2, period: "month".into(), amount: 30_000 });
    d.confirm = Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: 550 };
    d.purpose_text = "幫我整理每週摘要，並支付三項研究服務 — «résumé» 'ok' 😀".into();
    d.purpose_code = "TREAT".into();
    d.privacy = Privacy::Standard;
    let rich = issue(&d, &root).unwrap();
    put("rich", &rich);
    let mut d = base_draft(&root);
    d.holder = "app:reader".into();
    d.actions = s(&["files.read", "llm.call", "mail.draft"]);
    d.routine = s(&["mail.draft"]);
    d.resources = vec![Resource::new("folder", "*"), Resource::new("provider", "*")];
    d.limits.clear();
    d.confirm = Confirm::Never;
    d.privacy = Privacy::Public;
    d.purpose_text = "".into();
    d.period_zone = "UTC".into();
    let quiet = issue(&d, &root).unwrap();
    put("quiet", &quiet);
    let mut d = base_draft(&root);
    d.holder = "clinician:dr-lin".into();
    d.actions = s(&["health.read"]);
    d.resources = vec![Resource::new("record_kind", "medication"), Resource::new("record_kind", "allergy")];
    d.limits.clear();
    d.privacy = Privacy::Local;
    d.not_after = T0 + 3 * DAY;
    d.purpose_text = "Medication review before surgery".into();
    d.purpose_code = "TREAT".into();
    put("clinician", &issue(&d, &root).unwrap());
    let mut d = base_draft(&root);
    d.actions = s(&["llm.paid", "pay.spend"]);
    d.resources = vec![Resource::new("host", "a.example"), Resource::new("host", "b.example"), Resource::new("host", "c.example"), Resource::new("provider", "openrouter")];
    d.limits = vec![
        Limit { kind: "spend".into(), unit: "USDC".into(), decimals: 6, period: "day".into(), amount: 2_000_000 },
        Limit { kind: "spend".into(), unit: "USDC".into(), decimals: 6, period: "month".into(), amount: 30_000_000 },
    ];
    d.confirm = Confirm::OverAmount { unit: "USDC".into(), decimals: 6, amount: 500_000 };
    d.purpose_text = "Let my helper pay up to 2 USDC a day for these three services".into();
    let usdc = issue(&d, &root).unwrap();
    put("usdc", &usdc);

    // ---- narrowed, one kind at a time
    let n = |n: Narrowing, m: &Mandate| narrow(m, &n).unwrap();
    let spend = |period: &str, amount: i64| NarrowLimit { kind: "spend".into(), unit: "TWD".into(), period: period.into(), amount };
    let cap = |amount: i64| Cap { kind: "spend".into(), unit: "TWD".into(), amount };
    let base_actions = n(Narrowing { actions: s(&["files.read"]), ..Default::default() }, &base);
    put("base_actions", &base_actions);
    put("base_folder", &n(Narrowing { resources: vec![Resource::new("folder", "/docs/work/sub")], ..Default::default() }, &base));
    put("base_limit", &n(Narrowing { limits: vec![spend("day", 500)], ..Default::default() }, &base));
    put("base_limit_month", &n(Narrowing { limits: vec![spend("month", 5000)], ..Default::default() }, &base));
    put("base_cap", &n(Narrowing { caps: vec![cap(100)], ..Default::default() }, &base));
    put("base_window", &n(Narrowing { not_before: Some(T0 + DAY), not_after: Some(T0 + 2 * DAY), ..Default::default() }, &base));
    put("base_local", &n(Narrowing { privacy: Some(Privacy::Local), ..Default::default() }, &base));
    let base_job = n(Narrowing { job: Some("job-7".into()), ..Default::default() }, &base_actions);
    put("base_job", &base_job);
    put("base_job_tool", &n(Narrowing { tool: Some("call-19".into()), not_after: Some(T0 + 600), ..Default::default() }, &base_job));
    put("base_tool_long", &n(Narrowing { tool: Some("call-20".into()), not_after: Some(T0 + 2 * DAY), ..Default::default() }, &base));
    let long = n(Narrowing { tool: Some("call-20".into()), not_after: Some(T0 + 2 * DAY), ..Default::default() }, &base);
    put("base_tool_long_then_short", &n(Narrowing { not_after: Some(T0 + 600), ..Default::default() }, &long));
    let approval = Narrowing {
        actions: s(&["pay.spend"]),
        resources: vec![Resource::new("host", "api.example.com")],
        caps: vec![cap(12_000)],
        not_after: Some(T0 + 300),
        card: Some("card-3".into()),
        ..Default::default()
    };
    put("base_card", &n(approval.clone(), &base));
    put("base_card_other", &n(Narrowing { card: Some("card-4".into()), ..approval.clone() }, &base));
    put("rich_each", &n(Narrowing { confirm: Some(Confirm::EachWrite), ..Default::default() }, &rich));
    put("rich_over_lower", &n(Narrowing { confirm: Some(Confirm::OverAmount { unit: "TWD".into(), decimals: 2, amount: 100 }), ..Default::default() }, &rich));
    put("rich_unicode", &n(Narrowing { resources: vec![Resource::new("folder", "/文件/工作/報告"), Resource::new("calendar", "*"), Resource::new("host", "pay.example.com:8443")], ..Default::default() }, &rich));
    let step1 = n(Narrowing { actions: s(&["files.read", "files.write", "pay.spend"]), privacy: Some(Privacy::Private), ..Default::default() }, &rich);
    let step2 = n(Narrowing { resources: vec![Resource::new("folder", "/文件/工作/報告"), Resource::new("host", "api.example.com")], limits: vec![spend("day", 1000), spend("month", 9000)], ..Default::default() }, &step1);
    let step3 = n(Narrowing { actions: s(&["pay.spend"]), caps: vec![cap(300)], not_after: Some(T0 + 900), job: Some("job-7".into()), card: Some("card-9".into()), ..Default::default() }, &step2);
    put("rich_chain1", &step1);
    put("rich_chain2", &step2);
    put("rich_chain3", &step3);
    let usdc_cap = Cap { kind: "spend".into(), unit: "USDC".into(), amount: 1_250_000 };
    put("usdc_cap", &n(Narrowing { resources: vec![Resource::new("host", "b.example")], caps: vec![usdc_cap], ..Default::default() }, &usdc));
    put("quiet_sub", &n(Narrowing { resources: vec![Resource::new("folder", "/a b"), Resource::new("provider", "local-llm")], ..Default::default() }, &quiet));

    // ---- made by other software, or by an attacker
    let b = base.to_base64().unwrap();
    let narrowed = base_actions.to_base64().unwrap();
    tokens.insert("evil_facts".into(), raw_block(&narrowed, &root, "action(\"files.write\"); action(\"mail.send\"); resource(\"folder\", \"*\"); privacy_max(3);", &[]));
    tokens.insert("evil_rules".into(), raw_block(&narrowed, &root, "action($a) <- req_action($a); resource($k, $v) <- req_resource($k, $v);", &[]));
    tokens.insert("evil_check_true".into(), raw_block(&narrowed, &root, "check if true;", &[]));
    tokens.insert("evil_unsorted".into(), raw_block(&b, &root, "check if req_action(\"pay.spend\") or req_action(\"files.read\");", &[]));
    tokens.insert("evil_duplicate".into(), raw_block(&b, &root, "check if req_action(\"files.read\") or req_action(\"files.read\");", &[]));
    tokens.insert("evil_unknown_check".into(), raw_block(&b, &root, "check if req_weekday(\"monday\");", &[]));
    tokens.insert("evil_never".into(), raw_block(&b, &root, "check if confirm_applied(\"never\");", &[]));
    tokens.insert("evil_card_no_end".into(), raw_block(&b, &root, "check if req_card(\"card-1\");", &[]));
    tokens.insert("evil_unlimited_unit".into(), raw_block(&b, &root, "check if no_amount(\"spend\", \"USD\") or amount(\"spend\", \"USD\", $a), $a <= 5;", &[]));
    tokens.insert("evil_quote_job".into(), raw_block(&b, &root, "check if req_job({j});", &[("j", "x\") or req_job(\"y")]));
    // A string holding quotes that prints exactly like a narrowing to two actions. It reads
    // as that narrowing; the real check wants one odd action name. Both must pass, so
    // nothing passes: the token allows less than its sentence, never more.
    tokens.insert("evil_quote_actions".into(), raw_block(&b, &root, "check if req_action({a});", &[("a", "files.read\") or req_action(\"files.write")]));
    // a narrowing that lists an action the parent lacks: read as written, never allowed
    tokens.insert("foreign_wider_action".into(), raw_block(&b, &root, "check if req_action(\"files.read\") or req_action(\"mail.send\");", &[]));
    tokens.insert("foreign_disjoint".into(), raw_block(&narrowed, &root, "check if req_action(\"pay.spend\");", &[]));
    tokens.insert("foreign_two_jobs".into(), raw_block(&base_job.to_base64().unwrap(), &root, "check if req_job(\"job-8\");", &[]));
    {
        let biscuit = Biscuit::from_base64(&b, root.public()).unwrap();
        let req = biscuit.third_party_request().unwrap();
        let block = req.create_block(&other.private(), BlockBuilder::new().code("check if req_job(\"j\");").unwrap()).unwrap();
        tokens.insert("third_party".into(), biscuit.append_third_party(other.public(), block).unwrap().to_base64().unwrap());
        tokens.insert("sealed".into(), biscuit.seal().unwrap().to_base64().unwrap());
        let authority = biscuit.print_block_source(0).unwrap();
        // the same permission signed by a key that is not the root
        let mut theirs = base_draft(&other);
        theirs.purpose_text = "signed by another key".into();
        tokens.insert("other_root".into(), issue(&theirs, &other).unwrap().to_base64().unwrap());
        // signed by the root, but naming another issuer (with the id that matches that text)
        let mut named = base_draft(&root);
        named.issuer = public_key_text(&other);
        let named = propose_from_fields(named).unwrap(); // canonical order, so the id matches the text
        let wrong = authority.replace(&base.draft.issuer, &named.issuer).replace(&base.id, &mandate_id(&named));
        tokens.insert("wrong_issuer".into(), raw_authority(&root, &wrong));
        tokens.insert("bad_id".into(), raw_authority(&root, &authority.replace(&base.id, &format!("m1_{}", "0".repeat(64)))));
        tokens.insert("extra_fact".into(), raw_authority(&root, &format!("{authority}admin(true);\n")));
        tokens.insert("authority_check".into(), raw_authority(&root, &format!("{authority}check if true;\n")));
        let swapped = authority.replace("action(\"files.read\");\naction(\"files.write\");\n", "action(\"files.write\");\naction(\"files.read\");\n");
        assert_ne!(swapped, authority);
        tokens.insert("unsorted_authority".into(), raw_authority(&root, &swapped));
        tokens.insert("schema_2".into(), raw_authority(&root, &authority.replace("schema(\"muse.permission\", 1);", "schema(\"muse.permission\", 2);")));
        tokens.insert("foreign_schema".into(), raw_authority(&root, "right(\"file1\", \"read\");\n"));
        // never without the actions being read-only or routine
        let never = authority.replace("confirm(\"each_write\", \"\", 0, 0);", "confirm(\"never\", \"\", 0, 0);");
        let mut never_draft = base_draft(&root);
        never_draft.confirm = Confirm::Never;
        never_draft.limits.sort(); // canonical order, so the id matches the text and only the rule is wrong
        tokens.insert("never_for_writes".into(), raw_authority(&root, &never.replace(&base.id, &mandate_id(&never_draft))));
    }
    let good = tokens["base"].clone();
    let mid = good.len() / 2;
    let flipped = format!("{}{}{}", &good[..mid], if &good[mid..mid + 1] == "A" { "B" } else { "A" }, &good[mid + 1..]);
    tokens.insert("flipped_byte".into(), flipped);
    tokens.insert("truncated".into(), good[..good.len() - 40].to_string());
    tokens.insert("not_base64".into(), "this is not a token".to_string());
    tokens.insert("empty".into(), String::new());

    // ---- revocation entries
    let mut entries: BTreeMap<String, String> = BTreeMap::new();
    let sign = |id: &str, at: i64, key: &KeyPair| RevocationEntry::sign(id, at, key).unwrap().token;
    let ids = revocation_ids(&base_job);
    entries.insert("base_root".into(), sign(&ids[0], T0 + 100, &root));
    entries.insert("base_root_earlier".into(), sign(&ids[0], T0 + 50, &root));
    entries.insert("base_actions_block".into(), sign(&ids[1], T0 + 200, &root));
    entries.insert("base_job_block".into(), sign(&ids[2], T0 + 300, &root));
    entries.insert("unrelated".into(), sign(&"ab".repeat(64), T0, &root));
    entries.insert("forged_by_other".into(), sign(&ids[0], T0, &other));
    entries.insert("a_permission_is_not_an_entry".into(), good);
    let unrelated_entry = entries["unrelated"].clone();
    entries.insert("two_blocks".into(), raw_block(&unrelated_entry, &root, "check if true;", &[]));
    let unrelated = Biscuit::from_base64(&entries["unrelated"], root.public()).unwrap().print_block_source(0).unwrap();
    entries.insert("extra_line".into(), raw_authority(&root, &format!("{unrelated}note(\"x\");\n")));
    entries.insert("names_other_signer".into(), raw_authority(&root, &unrelated.replace(&public_key_text(&root), &public_key_text(&other))));

    let out = json!({
        "note": "Made once by core/tests/permissions_vectors.rs (generate_fixture_tokens). The keys are test-only and sign nothing else. Token bytes differ on every run, so do not regenerate unless the schema changes.",
        "made_by": "rust biscuit-auth 6.0.0",
        "root_private": ROOT, "root_public": public_key_text(&root),
        "other_private": OTHER, "other_public": public_key_text(&other),
        "t0": T0,
        "tokens": tokens,
        "revocations": entries,
    });
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("conformance/fixtures");
    fs::create_dir_all(&dir).unwrap();
    fs::write(dir.join("permission_tokens.json"), serde_json::to_string_pretty(&out).unwrap() + "\n").unwrap();
}
