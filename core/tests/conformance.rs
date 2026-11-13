//! Replays every vector in `conformance/*.json` (written by
//! `scripts/export_conformance.py` from the Python functions) and requires the
//! same result, bit for bit for floats.
//!
//! A vector is `{"name", "fn", "input", "expected"}`; `expected` is
//! `{"ok": value}` or `{"err": exception}`. A float JSON cannot hold is
//! `{"$float": "nan" | "inf" | "-inf"}`.

use std::collections::BTreeMap;
use std::fs;
use std::path::Path;

use muse_core::pyjson::PyJson;
use muse_core::ration::{Limit, Settings};
use muse_core::{cache, pytext, quota, ration, PyError};
use serde_json::{json, Value};

// ---------------------------------------------------------------- decoding inputs

fn special_float(v: &Value) -> Option<f64> {
    match v.get("$float")?.as_str()? {
        "nan" => Some(f64::NAN),
        "inf" => Some(f64::INFINITY),
        "-inf" => Some(f64::NEG_INFINITY),
        other => panic!("unknown $float {other}"),
    }
}

/// A float argument (Python accepts an int where a float is meant).
fn num(v: &Value) -> f64 {
    special_float(v).or_else(|| v.as_f64()).unwrap_or_else(|| panic!("not a number: {v}"))
}

fn opt_num(v: &Value) -> Option<f64> {
    (!v.is_null()).then(|| num(v))
}

fn opt_str(v: &Value) -> Option<&str> {
    v.as_str()
}

fn text(v: &Value) -> &str {
    v.as_str().unwrap_or_else(|| panic!("not a string: {v}"))
}

/// Python's `str(value)` for the scalar a header or duration may be given as;
/// None for null. The Rust functions take text, so the test does this step.
fn py_str(v: &Value) -> Option<String> {
    if let Some(x) = special_float(v) {
        return Some(pytext::float_repr(x));
    }
    match v {
        Value::Null => None,
        Value::String(s) => Some(s.clone()),
        Value::Bool(true) => Some("True".to_string()),
        Value::Bool(false) => Some("False".to_string()),
        Value::Number(n) if n.is_f64() => Some(pytext::float_repr(n.as_f64().unwrap())),
        Value::Number(n) => Some(n.to_string()),
        other => panic!("not a scalar: {other}"),
    }
}

fn limit_of(v: &Value) -> Limit {
    Limit {
        window: text(&v["window"]).to_string(),
        metric: text(&v["metric"]).to_string(),
        limit: v["limit"].as_i64().expect("limit"),
        remaining: v["remaining"].as_i64().expect("remaining"),
        reset_at: opt_num(&v["reset_at"]),
        source: text(&v["source"]).to_string(),
        window_start: opt_num(&v["window_start"]),
        scope: text(&v["scope"]).to_string(),
    }
}

fn settings_of(v: &Value) -> Settings {
    // Every field the struct has must be in the vector, and nothing else.
    let fields: Vec<&str> = v.as_object().expect("settings").keys().map(String::as_str).collect();
    let mirrored = serde_json::to_value(Settings::default()).unwrap();
    let ours: Vec<&str> = mirrored.as_object().unwrap().keys().map(String::as_str).collect();
    assert_eq!(fields, ours, "Settings fields differ from Python's");
    serde_json::from_value(v.clone()).expect("settings")
}

fn request_of(input: &Value) -> PyJson {
    match input.get("data_json") {
        Some(raw) => PyJson::parse(text(raw)).expect("data_json"),
        None => PyJson::from(&input["data"]),
    }
}

// ---------------------------------------------------------------- encoding results

fn float(x: f64) -> Value {
    if x.is_nan() {
        json!({"$float": "nan"})
    } else if x.is_infinite() {
        json!({ "$float": if x > 0.0 { "inf" } else { "-inf" } })
    } else {
        json!(x)
    }
}

fn opt_float(x: Option<f64>) -> Value {
    x.map(float).unwrap_or(Value::Null)
}

fn err_name(e: PyError) -> &'static str {
    match e {
        PyError::TimeOutOfRange => "TimeOutOfRange",
        PyError::Value => "ValueError",
        PyError::Overflow => "OverflowError",
        PyError::ZeroDivision => "ZeroDivisionError",
        PyError::Assertion => "AssertionError",
        PyError::Recursion => "RecursionError",
    }
}

fn outcome(result: Result<Value, PyError>) -> Value {
    match result {
        Ok(value) => json!({ "ok": value }),
        Err(e) => json!({ "err": err_name(e) }),
    }
}

/// Equal JSON, with numbers compared as f64 bit patterns (so 1 == 1.0, and
/// 0.0 != -0.0, and no tolerance).
fn same(a: &Value, b: &Value) -> bool {
    match (a, b) {
        (Value::Number(x), Value::Number(y)) => match (x.as_f64(), y.as_f64()) {
            (Some(x), Some(y)) => x.to_bits() == y.to_bits(),
            _ => false,
        },
        (Value::Array(x), Value::Array(y)) => x.len() == y.len() && x.iter().zip(y).all(|(x, y)| same(x, y)),
        (Value::Object(x), Value::Object(y)) => {
            x.len() == y.len() && x.iter().all(|(k, v)| y.get(k).map(|w| same(v, w)).unwrap_or(false))
        }
        _ => a == b,
    }
}

// ---------------------------------------------------------------- the functions

fn run(function: &str, input: &Value) -> Option<Value> {
    let now = || num(&input["now"]);
    Some(match function {
        // quota
        "parse_retry_after" => outcome(Ok(opt_float(quota::parse_retry_after(py_str(&input["value"]).as_deref(), now())))),
        "parse_duration" => outcome(Ok(opt_float(quota::parse_duration(py_str(&input["value"]).as_deref())))),
        "parse_reset" => outcome(Ok(opt_float(quota::parse_reset(py_str(&input["value"]).as_deref(), now())))),
        "next_midnight_pacific" => outcome(quota::next_midnight_pacific(now()).map(float)),
        "next_midnight_utc" => outcome(quota::next_midnight_utc(now()).map(float)),
        "next_month_utc" => outcome(quota::next_month_utc(now()).map(float)),
        "month_start" => outcome(quota::month_start(now()).map(float)),
        "day_start" => outcome(quota::day_start(now(), opt_str(&input["provider_id"])).map(float)),
        "iso" => outcome(quota::iso(opt_num(&input["ts"])).map(|s| json!(s))),
        "classify_failure" => {
            // Python takes any object as a status and ignores what is not an int.
            let status = if input["status"].is_i64() { input["status"].as_i64() } else { None };
            let headers: Option<Vec<(String, Option<String>)>> = input["headers"].as_array().map(|pairs| {
                pairs.iter().map(|pair| (text(&pair[0]).to_string(), py_str(&pair[1]))).collect()
            });
            outcome(
                quota::classify_failure(
                    text(&input["provider_id"]),
                    status,
                    headers.as_deref(),
                    opt_str(&input["body_text"]),
                    now(),
                )
                .map(|block| match block {
                    Some(b) => json!({"scope": b.scope, "kind": b.kind, "until": float(b.until), "reason": b.reason}),
                    None => Value::Null,
                }),
            )
        }
        // ration
        "previous_midnight_pacific" => outcome(ration::previous_midnight_pacific(now()).map(float)),
        "window_bounds" => outcome(
            ration::window_bounds(
                text(&input["window"]),
                text(&input["provider_id"]),
                now(),
                opt_num(&input["reset_at"]),
                input["observed"].as_bool().expect("observed"),
            )
            .map(|bounds| match bounds {
                Some((start, end)) => json!([float(start), float(end)]),
                None => Value::Null,
            }),
        ),
        "window_reset" => outcome(ration::window_reset(text(&input["window"]), text(&input["provider_id"]), now()).map(float)),
        "burn_rate" => outcome(Ok(opt_float(ration::burn_rate(
            opt_num(&input["units"]),
            input["calls"].as_i64(),
            num(&input["window_s"]),
            input["min_calls"].as_i64().expect("min_calls"),
        )))),
        "pace_position" => outcome(ration::pace_position(&limit_of(&input["limit"]), now(), opt_num(&input["rate"])).map(|p| {
            json!({
                "applies": p.applies,
                "reason": p.reason,
                "remaining_fraction": float(p.remaining_fraction),
                "expected_remaining_fraction": opt_float(p.expected_remaining_fraction),
                "surplus": opt_float(p.surplus),
                "projected_leftover": opt_float(p.projected_leftover),
                "window_start": opt_float(p.window_start),
                "reset_at": opt_float(p.reset_at),
            })
        })),
        "pace_release_at" => outcome(
            ration::pace_release_at(&limit_of(&input["limit"]), num(&input["margin"]), &settings_of(&input["settings"]), now())
                .map(float),
        ),
        "pace_holds" => outcome(Ok(json!(ration::pace_holds(&limit_of(&input["limit"]), &settings_of(&input["settings"]), now())))),
        "options_trailer" => outcome(
            ration::options_trailer(opt_num(&input["wait_at"]), input["handoff"].as_bool().expect("handoff"), opt_num(&input["pay_usd"]))
                .map(|s| json!(s)),
        ),
        "parse_options" => outcome(ration::parse_options(text(&input["message"])).map(|o| {
            let mut out = serde_json::Map::new();
            if let Some(wait) = o.wait {
                out.insert("wait".to_string(), json!(wait));
            }
            if let Some(pay) = o.pay {
                out.insert("pay".to_string(), float(pay));
            }
            if o.handoff {
                out.insert("handoff".to_string(), json!(true));
            }
            Value::Object(out)
        })),
        // cache
        "cacheable" => outcome(
            cache::cacheable(&request_of(input), input["deterministic"].as_bool().expect("deterministic"))
                .map(|(ok, why)| json!([ok, why])),
        ),
        "cache_key" => outcome(Ok(json!(cache::cache_key(
            text(&input["alias"]),
            &request_of(input),
            text(&input["privacy"]),
            text(&input["app_default_privacy"]),
        )))),
        _ => return None,
    })
}

/// Every function this slice ports, by the file its vectors live in.
const EXPECTED: [(&str, &[&str]); 3] = [
    (
        "quota.json",
        &[
            "parse_retry_after", "parse_duration", "parse_reset", "next_midnight_pacific", "next_midnight_utc",
            "next_month_utc", "month_start", "day_start", "iso", "classify_failure",
        ],
    ),
    (
        "ration.json",
        &[
            "previous_midnight_pacific", "window_bounds", "window_reset", "burn_rate", "pace_position",
            "pace_release_at", "pace_holds", "options_trailer", "parse_options",
        ],
    ),
    ("cache.json", &["cacheable", "cache_key"]),
];

/// Vector files replayed by another test file (records.json: tests/records_conformance.rs).
const OTHER_TESTS: &[&str] = &["records.json"];

#[test]
fn every_vector_matches_python() {
    let dir = Path::new(env!("CARGO_MANIFEST_DIR")).join("conformance");
    let mut files: Vec<String> = fs::read_dir(&dir)
        .expect("core/conformance")
        .map(|entry| entry.unwrap().file_name().into_string().unwrap())
        .filter(|name| name.ends_with(".json") && !OTHER_TESTS.contains(&name.as_str()))
        .collect();
    files.sort();
    let mut known: Vec<&str> = EXPECTED.iter().map(|(file, _)| *file).collect();
    known.sort();
    assert_eq!(files, known, "the files in core/conformance are not the ones this test knows");

    let mut failures = Vec::new();
    let mut total = 0;
    for (file, functions) in EXPECTED {
        let raw = fs::read_to_string(dir.join(file)).unwrap();
        let vectors: Vec<Value> = serde_json::from_str(&raw).unwrap_or_else(|e| panic!("{file}: {e}"));
        let mut counts: BTreeMap<String, usize> = BTreeMap::new();
        for vector in &vectors {
            let (name, function) = (text(&vector["name"]), text(&vector["fn"]));
            *counts.entry(function.to_string()).or_default() += 1;
            total += 1;
            match run(function, &vector["input"]) {
                None => failures.push(format!("{file}: {name}: no Rust function for {function}")),
                Some(got) if !same(&got, &vector["expected"]) => {
                    failures.push(format!("{file}: {name}\n  input    {}\n  expected {}\n  got      {got}", vector["input"], vector["expected"]))
                }
                Some(_) => {}
            }
        }
        for function in functions {
            assert!(counts.contains_key(*function), "{file} has no vectors for {function}");
        }
        assert_eq!(counts.len(), functions.len(), "{file} has vectors for functions this test does not expect: {counts:?}");
        println!("{file}: {} vectors {counts:?}", vectors.len());
    }
    println!("total: {total} vectors");
    assert!(total > 1000, "suspiciously few vectors: {total}");
    if !failures.is_empty() {
        let shown: Vec<&str> = failures.iter().take(40).map(String::as_str).collect();
        panic!("{} of {total} vectors differ from Python:\n{}", failures.len(), shown.join("\n"));
    }
}
