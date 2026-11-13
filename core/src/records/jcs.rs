//! Canonical JSON for records: RFC 8785 (JCS), restricted to what a record may hold.
//!
//! Allowed values: null, true, false, integers in ±(2^53 − 1), strings, arrays
//! and objects. Floats are refused: a record holds no fractional number, and
//! for integers in this range the JCS number form is plain decimal, so the
//! output is what any RFC 8785 implementation gives for the same data.
//!
//! Rules (RFC 8785): no whitespace; object members sorted by key, comparing
//! keys as UTF-16 code units; strings escape `"` and `\`, use `\b \f \n \r \t`,
//! write every other character below U+0020 as `\u00xx` (lower-case hex) and
//! everything else as itself in UTF-8.
//!
//! `crate::pyjson` is not reused: it writes Python's `json.dumps` form (ASCII
//! escapes, floats, Python's separators), which is a different byte string.
//!
//! The Python twin is `muse/records/jcs.py`.

use std::fmt;

use serde::de::{Deserialize, Deserializer, MapAccess, SeqAccess, Visitor};
use serde_json::{Map, Value};

use super::{Kind, Problem};

/// The largest integer a record may hold: 2^53 − 1.
pub const MAX_INT: u64 = (1 << 53) - 1;
/// How many containers may be nested inside the outermost value.
pub const MAX_DEPTH: usize = 32;

fn write_string(text: &str, out: &mut Vec<u8>) {
    out.push(b'"');
    for ch in text.chars() {
        match ch {
            '"' => out.extend_from_slice(b"\\\""),
            '\\' => out.extend_from_slice(b"\\\\"),
            '\u{08}' => out.extend_from_slice(b"\\b"),
            '\t' => out.extend_from_slice(b"\\t"),
            '\n' => out.extend_from_slice(b"\\n"),
            '\u{0c}' => out.extend_from_slice(b"\\f"),
            '\r' => out.extend_from_slice(b"\\r"),
            c if (c as u32) < 0x20 => out.extend_from_slice(format!("\\u{:04x}", c as u32).as_bytes()),
            c => out.extend_from_slice(c.encode_utf8(&mut [0u8; 4]).as_bytes()),
        }
    }
    out.push(b'"');
}

fn write(value: &Value, out: &mut Vec<u8>, depth: usize) -> Result<(), Problem> {
    if depth > MAX_DEPTH {
        return Err(Kind::Malformed.into());
    }
    match value {
        Value::Null => out.extend_from_slice(b"null"),
        Value::Bool(true) => out.extend_from_slice(b"true"),
        Value::Bool(false) => out.extend_from_slice(b"false"),
        Value::Number(n) => {
            if let Some(u) = n.as_u64() {
                if u > MAX_INT {
                    return Err(Kind::Malformed.into());
                }
                out.extend_from_slice(u.to_string().as_bytes());
            } else if let Some(i) = n.as_i64() {
                if i.unsigned_abs() > MAX_INT {
                    return Err(Kind::Malformed.into());
                }
                out.extend_from_slice(i.to_string().as_bytes());
            } else {
                return Err(Kind::Malformed.into()); // a float
            }
        }
        Value::String(s) => write_string(s, out),
        Value::Array(items) => {
            out.push(b'[');
            for (i, item) in items.iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write(item, out, depth + 1)?;
            }
            out.push(b']');
        }
        Value::Object(map) => {
            let mut keys: Vec<&String> = map.keys().collect();
            keys.sort_by(|a, b| a.encode_utf16().cmp(b.encode_utf16()));
            out.push(b'{');
            for (i, key) in keys.into_iter().enumerate() {
                if i > 0 {
                    out.push(b',');
                }
                write_string(key, out);
                out.push(b':');
                write(&map[key], out, depth + 1)?;
            }
            out.push(b'}');
        }
    }
    Ok(())
}

/// The canonical bytes of `value`; `Malformed` for a float, an integer outside
/// ±(2^53 − 1), or nesting deeper than [`MAX_DEPTH`].
pub fn canonicalize(value: &Value) -> Result<Vec<u8>, Problem> {
    let mut out = Vec::new();
    write(value, &mut out, 0)?;
    Ok(out)
}

// ---------------------------------------------------------------- strict reading

struct Strict(Value);

struct StrictVisitor;

impl<'de> Visitor<'de> for StrictVisitor {
    type Value = Strict;

    fn expecting(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str("JSON a record may hold")
    }

    fn visit_bool<E>(self, v: bool) -> Result<Strict, E> {
        Ok(Strict(Value::Bool(v)))
    }

    fn visit_i64<E: serde::de::Error>(self, v: i64) -> Result<Strict, E> {
        if v.unsigned_abs() > MAX_INT {
            return Err(E::custom("integer out of range"));
        }
        Ok(Strict(Value::from(v)))
    }

    fn visit_u64<E: serde::de::Error>(self, v: u64) -> Result<Strict, E> {
        if v > MAX_INT {
            return Err(E::custom("integer out of range"));
        }
        Ok(Strict(Value::from(v)))
    }

    fn visit_f64<E: serde::de::Error>(self, _: f64) -> Result<Strict, E> {
        // serde_json reads `1.0`, `1e3`, `-0` and integers beyond 64 bits as floats.
        Err(E::custom("a fractional, exponent or oversized number"))
    }

    fn visit_str<E>(self, v: &str) -> Result<Strict, E> {
        Ok(Strict(Value::String(v.to_string())))
    }

    fn visit_string<E>(self, v: String) -> Result<Strict, E> {
        Ok(Strict(Value::String(v)))
    }

    fn visit_unit<E>(self) -> Result<Strict, E> {
        Ok(Strict(Value::Null))
    }

    fn visit_seq<A: SeqAccess<'de>>(self, mut seq: A) -> Result<Strict, A::Error> {
        let mut items = Vec::new();
        while let Some(Strict(item)) = seq.next_element()? {
            items.push(item);
        }
        Ok(Strict(Value::Array(items)))
    }

    fn visit_map<A: MapAccess<'de>>(self, mut access: A) -> Result<Strict, A::Error> {
        let mut map = Map::new();
        while let Some(key) = access.next_key::<String>()? {
            let Strict(value) = access.next_value()?;
            if map.insert(key, value).is_some() {
                return Err(serde::de::Error::custom("a repeated key"));
            }
        }
        Ok(Strict(Value::Object(map)))
    }
}

impl<'de> Deserialize<'de> for Strict {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Strict, D::Error> {
        d.deserialize_any(StrictVisitor)
    }
}

/// Read JSON the way a verifier must: no repeated keys, no fractional or
/// exponent numbers, no `-0`, no integer outside ±(2^53 − 1), no lone surrogate
/// escapes, no byte-order mark, nothing after the value. Anything else is `Malformed`.
pub fn loads_strict(text: &str) -> Result<Value, Problem> {
    serde_json::from_str::<Strict>(text).map(|s| s.0).map_err(|_| Kind::Malformed.into())
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn canon(v: Value) -> String {
        String::from_utf8(canonicalize(&v).unwrap()).unwrap()
    }

    #[test]
    fn sorts_keys_by_utf16_code_units() {
        // U+1F600 is the surrogate pair D83D DE00, which sorts before U+FFFF in UTF-16 (and after it by code point).
        assert_eq!(canon(json!({"\u{ffff}": 1, "\u{1f600}": 2, "a": 3})), "{\"a\":3,\"\u{1f600}\":2,\"\u{ffff}\":1}");
    }

    #[test]
    fn rfc8785_sorting_example() {
        let v = json!({"\u{20ac}": "Euro Sign", "\r": "Carriage Return", "\u{fb33}": "Hebrew Letter Dalet With Dagesh",
            "1": "One", "\u{1f600}": "Emoji: Grinning Face", "\u{80}": "Control", "\u{f6}": "Latin Small Letter O With Diaeresis"});
        let order: Vec<&str> = vec!["Carriage Return", "One", "Control", "Latin Small Letter O With Diaeresis", "Euro Sign",
            "Emoji: Grinning Face", "Hebrew Letter Dalet With Dagesh"];
        let text = canon(v);
        let mut last = 0;
        for name in order {
            let at = text.find(name).unwrap();
            assert!(at >= last, "{name} is out of order in {text}");
            last = at;
        }
    }

    #[test]
    fn escapes_only_what_jcs_escapes() {
        assert_eq!(canon(json!("\u{0}\u{1f}\u{8}\t\n\u{c}\r\"\\/\u{7f}\u{2028}é")), "\"\\u0000\\u001f\\b\\t\\n\\f\\r\\\"\\\\/\u{7f}\u{2028}é\"");
    }

    #[test]
    fn integers_only_and_in_range() {
        assert_eq!(canon(json!([0, -1, 9007199254740991u64, -9007199254740991i64])), "[0,-1,9007199254740991,-9007199254740991]");
        for bad in [json!(1.0), json!(0.5), json!(9007199254740992u64), json!(-9007199254740992i64), json!(u64::MAX)] {
            assert!(canonicalize(&bad).is_err(), "{bad}");
        }
    }

    #[test]
    fn strict_reader_refuses_what_parsers_disagree_on() {
        for bad in ["{\"a\":1,\"a\":2}", "[1.0]", "[1e2]", "[-0]", "[9007199254740992]", "[\"\\ud83d\"]", "\u{feff}[]", "[] x", "", "[NaN]"] {
            assert!(loads_strict(bad).is_err(), "{bad}");
        }
        assert_eq!(loads_strict(" {\"b\": [1, \"\\u00e9\"], \"a\": null} ").unwrap(), json!({"a": null, "b": [1, "é"]}));
    }
}
