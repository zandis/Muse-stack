//! Behaviour the conformance vectors cannot carry (inputs too large for a
//! committed file, platform-dependent Python results) and the Rust-only API.

use muse_core::pyjson::{JsonError, PyJson, MAX_DEPTH};
use muse_core::quota::{self, Block};
use muse_core::{cache, pytext, PyError};

#[test]
fn json_nested_too_deeply_is_a_recursion_error() {
    // Python raises RecursionError out of classify_failure for such a body
    // (at about 10,000 levels on CPython 3.13); the port stops at MAX_DEPTH.
    let fine = format!("{}1{}", "{\"a\":".repeat(MAX_DEPTH), "}".repeat(MAX_DEPTH));
    assert!(PyJson::parse(&fine).is_ok());
    assert!(matches!(quota::classify_failure("x", Some(429), None, Some(&fine), 0.0), Ok(Some(_))));
    let deep = "{\"a\":".repeat(MAX_DEPTH + 1);
    assert_eq!(PyJson::raw_decode(&deep, 0), Err(JsonError::TooDeep));
    assert_eq!(quota::classify_failure("x", Some(429), None, Some(&deep), 0.0), Err(PyError::Recursion));
    assert_eq!(quota::classify_failure("x", Some(500), None, Some(&deep), 0.0), Err(PyError::Recursion));
    // Without a status nothing is read, as in Python.
    assert_eq!(quota::classify_failure("x", None, None, Some(&deep), 0.0), Ok(None));
    let arrays = format!("{{\"a\":{}", "[".repeat(200_000));
    assert_eq!(quota::classify_failure("x", Some(429), None, Some(&arrays), 0.0), Err(PyError::Recursion));
}

#[test]
fn iso_does_not_pad_years_below_1000() {
    // CPython on glibc: datetime(385, 7, 25).strftime("%Y") == "385".
    assert_eq!(quota::iso(Some(-50_000_000_000.0)), Ok(Some("385-07-25T07:06:40Z".to_string())));
    assert_eq!(quota::iso(Some(-62_135_596_800.0)), Ok(Some("1-01-01T00:00:00Z".to_string())));
}

#[test]
fn block_key() {
    let block = |scope: &str| Block { scope: scope.into(), kind: "cooldown".into(), until: 1.0, reason: "r".into() };
    assert_eq!(block("provider").key("groq", Some("groq:groq/x")), "provider:groq");
    assert_eq!(block("upstream").key("groq", Some("groq:groq/x")), "groq:groq/x");
    assert_eq!(block("upstream").key("groq", None), "provider:groq");
    assert_eq!(block("upstream").key("groq", Some("")), "provider:groq");
}

#[test]
fn headers_are_normalised_like_the_python_dict() {
    let pairs = vec![
        ("Retry-After".to_string(), Some("5".to_string())),
        (" X-Other ".to_string(), None),
        ("llm_provider-Retry-After".to_string(), Some("9".to_string())),
        ("RETRY-AFTER".to_string(), Some("7".to_string())),
    ];
    let headers = quota::normalise_headers(Some(&pairs));
    assert_eq!(headers.get("retry-after"), Some("9"));
    assert_eq!(headers.get("x-other"), None);
    assert_eq!(headers.iter().count(), 1);
    assert_eq!(quota::normalise_headers(None).iter().count(), 0);
}

#[test]
fn float_repr_is_pythons() {
    for (x, text) in [
        (0.1, "0.1"), (1.0, "1.0"), (-0.0, "-0.0"), (1e15, "1000000000000000.0"), (1e16, "1e+16"), (1.5e16, "1.5e+16"),
        (1e-4, "0.0001"), (1e-5, "1e-05"), (1.5e-7, "1.5e-07"), (5e-324, "5e-324"), (1.7976931348623157e308, "1.7976931348623157e+308"),
        (123456.789, "123456.789"), (0.30000000000000004, "0.30000000000000004"), (f64::NAN, "nan"), (f64::NEG_INFINITY, "-inf"),
    ] {
        assert_eq!(pytext::float_repr(x), text);
    }
}

#[test]
fn digits_and_spaces_follow_python() {
    assert_eq!(pytext::py_int(" +1_2 "), Some(12));
    assert_eq!(pytext::py_int("\u{661}\u{662}"), Some(12));
    assert_eq!(pytext::py_int("\u{b2}"), None); // superscript two: isdigit() but not int()
    assert!(pytext::is_digit('\u{b2}') && pytext::is_digit('\u{ff11}') && !pytext::is_digit('x'));
    assert_eq!(pytext::py_int("\u{1c}5"), None);
    assert_eq!(pytext::strip("\u{1c} a\u{a0}"), "a");
    assert_eq!(pytext::split_ws("a\u{1f}b  c\u{3000}"), vec!["a", "b", "c"]);
    assert_eq!(pytext::py_float("1__0"), None);
    assert_eq!(pytext::py_float("-inf"), Some(f64::NEG_INFINITY));
}

#[test]
fn a_serde_json_value_hashes_like_its_text() {
    let text = r#"{"messages": [{"role": "user", "content": "héllo \u2028"}], "temperature": 0.0, "seed": 18446744073709551615}"#;
    let from_text = PyJson::parse(text).unwrap();
    let from_value = PyJson::from(&serde_json::from_str::<serde_json::Value>(text).unwrap());
    // Same content; only the order of object keys differs (serde_json sorts them).
    assert_eq!(from_text.dumps_canonical(), from_value.dumps_canonical());
    assert_eq!(
        cache::cache_key("fast", &from_text, "standard", "standard"),
        cache::cache_key("fast", &from_value, "standard", "standard")
    );
    // A request that is not an object has no messages and no parameters.
    assert_eq!(cache::cacheable(&PyJson::Null, true), Ok((false, "not a chat request".to_string())));
    assert_eq!(
        cache::cache_key("a", &PyJson::List(vec![]), "p", "q"),
        cache::cache_key("a", &PyJson::Dict(vec![]), "p", "q")
    );
}

#[test]
fn json_reader_rejects_what_python_rejects() {
    for bad in ["", "{", "{\"a\":1,}", "[1,]", "01", "1.", ".5", "+1", "'a'", "\"\t\"", "\"\\x\"", "nul", "{\"a\" 1}", "1 2", "-"] {
        assert_eq!(PyJson::parse(bad), Err(JsonError::Invalid), "{bad:?}");
    }
    assert_eq!(PyJson::parse(" [1, -0, 1.0, 1e2, NaN] ").map(|v| v.dumps_canonical()), Ok("[1,0,1.0,100.0,NaN]".to_string()));
    assert_eq!(PyJson::raw_decode("x{\"a\": [true, null]} tail", 1).map(|(_, end)| end), Ok(20));
}
