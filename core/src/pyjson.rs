//! JSON as Python's `json` module reads and writes it.
//!
//! Two things in the port depend on the exact rules: `classify_failure` finds
//! JSON objects inside an error message with `JSONDecoder.raw_decode`, and
//! `cache_key` hashes `json.dumps(..., sort_keys=True, separators=(",", ":"),
//! ensure_ascii=False)`. serde_json differs from both in ways that would change
//! results (it rejects `NaN`, reads `-0` as a float, turns integers above
//! u64 into floats, and formats floats differently), so the value type and the
//! reader/writer live here.

use std::collections::HashMap;

use crate::pytext;

/// Containers nested deeper than this are refused (Python raises RecursionError
/// at an interpreter-dependent depth, near 10,000 on CPython 3.13).
pub const MAX_DEPTH: usize = 1000;

/// CPython's default `sys.int_info.default_max_str_digits`.
const MAX_INT_DIGITS: usize = 4300;

/// A JSON value with Python's distinctions: integers are exact (kept as their
/// decimal text), floats are f64 and may be NaN or infinite, and an object keeps
/// its keys in first-seen order with the last value for a repeated key.
#[derive(Debug, Clone, PartialEq)]
pub enum PyJson {
    Null,
    Bool(bool),
    /// Decimal digits with an optional leading `-`; never `-0`.
    Int(String),
    Float(f64),
    Str(String),
    List(Vec<PyJson>),
    Dict(Vec<(String, PyJson)>),
}

/// Why text could not be read.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum JsonError {
    /// Not JSON at this position (Python: `json.JSONDecodeError`, a ValueError).
    Invalid,
    /// Nested deeper than [`MAX_DEPTH`] (Python: RecursionError, at its own depth).
    TooDeep,
}

impl PyJson {
    /// `json.loads(text)`: one value with optional whitespace around it.
    pub fn parse(text: &str) -> Result<PyJson, JsonError> {
        let bytes = text.as_bytes();
        let start = skip_ws(bytes, 0);
        let (value, end) = Self::raw_decode(text, start)?;
        if skip_ws(bytes, end) != bytes.len() {
            return Err(JsonError::Invalid);
        }
        Ok(value)
    }

    /// `json.JSONDecoder().raw_decode(text, index)`: the value starting exactly
    /// at byte `index` and the byte index just after it.
    pub fn raw_decode(text: &str, index: usize) -> Result<(PyJson, usize), JsonError> {
        Reader { text, bytes: text.as_bytes() }.value(index, 0)
    }

    /// `dict.get(key)` on an object; None for a missing key or a non-object.
    pub fn get(&self, key: &str) -> Option<&PyJson> {
        match self {
            PyJson::Dict(items) => items.iter().find(|(k, _)| k == key).map(|(_, v)| v),
            _ => None,
        }
    }

    /// `json.dumps(self, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`.
    pub fn dumps_canonical(&self) -> String {
        let mut out = String::new();
        self.write_canonical(&mut out);
        out
    }

    fn write_canonical(&self, out: &mut String) {
        match self {
            PyJson::Null => out.push_str("null"),
            PyJson::Bool(true) => out.push_str("true"),
            PyJson::Bool(false) => out.push_str("false"),
            PyJson::Int(text) => out.push_str(text),
            PyJson::Float(x) => out.push_str(&float_json(*x)),
            PyJson::Str(s) => write_string(s, out),
            PyJson::List(items) => {
                out.push('[');
                for (i, item) in items.iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    item.write_canonical(out);
                }
                out.push(']');
            }
            PyJson::Dict(items) => {
                // Python sorts str keys by code point, which is UTF-8 byte order.
                let mut sorted: Vec<&(String, PyJson)> = items.iter().collect();
                sorted.sort_by(|a, b| a.0.as_bytes().cmp(b.0.as_bytes()));
                out.push('{');
                for (i, (key, value)) in sorted.into_iter().enumerate() {
                    if i > 0 {
                        out.push(',');
                    }
                    write_string(key, out);
                    out.push(':');
                    value.write_canonical(out);
                }
                out.push('}');
            }
        }
    }

    /// Whether `needle` (lower-case ASCII letters) occurs in `str(self).lower()`.
    /// For a container, Python's repr shows every key and string inside it, and
    /// letters cannot be joined across the quotes and separators between them.
    pub(crate) fn str_lower_contains(&self, needle: &str) -> bool {
        match self {
            PyJson::Str(s) => s.to_lowercase().contains(needle),
            PyJson::List(items) => items.iter().any(|item| item.str_lower_contains(needle)),
            PyJson::Dict(items) => items
                .iter()
                .any(|(k, v)| k.to_lowercase().contains(needle) || v.str_lower_contains(needle)),
            _ => false,
        }
    }

    /// `str(self)` for a scalar. A container gives a stand-in that, like its
    /// Python repr, starts with a bracket and so is never a number or duration.
    pub(crate) fn scalar_str(&self) -> String {
        match self {
            PyJson::Null => "None".to_string(),
            PyJson::Bool(true) => "True".to_string(),
            PyJson::Bool(false) => "False".to_string(),
            PyJson::Int(text) => text.clone(),
            PyJson::Float(x) => pytext::float_repr(*x),
            PyJson::Str(s) => s.clone(),
            PyJson::List(_) => "[...]".to_string(),
            PyJson::Dict(_) => "{...}".to_string(),
        }
    }
}

/// From an already-parsed serde_json value. Exact for everything serde_json can
/// hold; what it has already lost (`-0` read as a float, integers above u64
/// read as floats) stays lost, so hash request text with [`PyJson::parse`] when
/// the key must match Python's for such input.
impl From<&serde_json::Value> for PyJson {
    fn from(value: &serde_json::Value) -> Self {
        use serde_json::Value;
        match value {
            Value::Null => PyJson::Null,
            Value::Bool(b) => PyJson::Bool(*b),
            Value::Number(n) => {
                if let Some(i) = n.as_i64() {
                    PyJson::Int(i.to_string())
                } else if let Some(u) = n.as_u64() {
                    PyJson::Int(u.to_string())
                } else {
                    PyJson::Float(n.as_f64().unwrap_or(f64::NAN))
                }
            }
            Value::String(s) => PyJson::Str(s.clone()),
            Value::Array(items) => PyJson::List(items.iter().map(PyJson::from).collect()),
            Value::Object(map) => PyJson::Dict(map.iter().map(|(k, v)| (k.clone(), PyJson::from(v))).collect()),
        }
    }
}

fn float_json(x: f64) -> String {
    if x.is_nan() {
        "NaN".to_string()
    } else if x.is_infinite() {
        if x < 0.0 { "-Infinity" } else { "Infinity" }.to_string()
    } else {
        pytext::float_repr(x)
    }
}

fn write_string(s: &str, out: &mut String) {
    out.push('"');
    for c in s.chars() {
        match c {
            '"' => out.push_str("\\\""),
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            '\u{8}' => out.push_str("\\b"),
            '\u{c}' => out.push_str("\\f"),
            c if (c as u32) < 0x20 => out.push_str(&format!("\\u{:04x}", c as u32)),
            c => out.push(c),
        }
    }
    out.push('"');
}

fn skip_ws(bytes: &[u8], mut i: usize) -> usize {
    while i < bytes.len() && matches!(bytes[i], b' ' | b'\t' | b'\n' | b'\r') {
        i += 1;
    }
    i
}

struct Reader<'a> {
    text: &'a str,
    bytes: &'a [u8],
}

impl Reader<'_> {
    fn at(&self, i: usize) -> Option<u8> {
        self.bytes.get(i).copied()
    }

    fn literal(&self, i: usize, word: &str) -> bool {
        self.bytes.get(i..i + word.len()) == Some(word.as_bytes())
    }

    fn value(&self, i: usize, depth: usize) -> Result<(PyJson, usize), JsonError> {
        match self.at(i).ok_or(JsonError::Invalid)? {
            b'"' => self.string(i + 1).map(|(s, end)| (PyJson::Str(s), end)),
            b'{' => self.object(i + 1, depth + 1),
            b'[' => self.array(i + 1, depth + 1),
            b'n' if self.literal(i, "null") => Ok((PyJson::Null, i + 4)),
            b't' if self.literal(i, "true") => Ok((PyJson::Bool(true), i + 4)),
            b'f' if self.literal(i, "false") => Ok((PyJson::Bool(false), i + 5)),
            b'N' if self.literal(i, "NaN") => Ok((PyJson::Float(f64::NAN), i + 3)),
            b'I' if self.literal(i, "Infinity") => Ok((PyJson::Float(f64::INFINITY), i + 8)),
            b'-' if self.literal(i, "-Infinity") => Ok((PyJson::Float(f64::NEG_INFINITY), i + 9)),
            _ => self.number(i),
        }
    }

    fn object(&self, mut i: usize, depth: usize) -> Result<(PyJson, usize), JsonError> {
        if depth > MAX_DEPTH {
            return Err(JsonError::TooDeep);
        }
        let mut items: Vec<(String, PyJson)> = Vec::new();
        let mut seen: HashMap<String, usize> = HashMap::new();
        i = skip_ws(self.bytes, i);
        if self.at(i) == Some(b'}') {
            return Ok((PyJson::Dict(items), i + 1));
        }
        loop {
            if self.at(i) != Some(b'"') {
                return Err(JsonError::Invalid);
            }
            let (key, after_key) = self.string(i + 1)?;
            i = skip_ws(self.bytes, after_key);
            if self.at(i) != Some(b':') {
                return Err(JsonError::Invalid);
            }
            i = skip_ws(self.bytes, i + 1);
            let (value, after_value) = self.value(i, depth)?;
            match seen.get(&key) {
                Some(slot) => items[*slot].1 = value,
                None => {
                    seen.insert(key.clone(), items.len());
                    items.push((key, value));
                }
            }
            i = skip_ws(self.bytes, after_value);
            match self.at(i) {
                Some(b'}') => return Ok((PyJson::Dict(items), i + 1)),
                Some(b',') => i = skip_ws(self.bytes, i + 1),
                _ => return Err(JsonError::Invalid),
            }
        }
    }

    fn array(&self, mut i: usize, depth: usize) -> Result<(PyJson, usize), JsonError> {
        if depth > MAX_DEPTH {
            return Err(JsonError::TooDeep);
        }
        let mut items = Vec::new();
        i = skip_ws(self.bytes, i);
        if self.at(i) == Some(b']') {
            return Ok((PyJson::List(items), i + 1));
        }
        loop {
            let (value, after_value) = self.value(i, depth)?;
            items.push(value);
            i = skip_ws(self.bytes, after_value);
            match self.at(i) {
                Some(b']') => return Ok((PyJson::List(items), i + 1)),
                Some(b',') => i = skip_ws(self.bytes, i + 1),
                _ => return Err(JsonError::Invalid),
            }
        }
    }

    fn hex4(&self, i: usize) -> Option<u32> {
        let digits = self.bytes.get(i..i + 4)?;
        let mut value = 0u32;
        for b in digits {
            value = value * 16 + (*b as char).to_digit(16)?;
        }
        Some(value)
    }

    /// The string whose opening quote is just before `i` (strict mode: no raw
    /// control characters). A lone surrogate escape, which Python keeps and a
    /// Rust string cannot hold, becomes U+FFFD; nothing ported here can tell.
    fn string(&self, mut i: usize) -> Result<(String, usize), JsonError> {
        let mut out = String::new();
        loop {
            let start = i;
            while let Some(b) = self.at(i) {
                if b == b'"' || b == b'\\' || b < 0x20 {
                    break;
                }
                i += 1;
            }
            out.push_str(&self.text[start..i]);
            match self.at(i).ok_or(JsonError::Invalid)? {
                b'"' => return Ok((out, i + 1)),
                b'\\' => {}
                _ => return Err(JsonError::Invalid),
            }
            let escape = self.at(i + 1).ok_or(JsonError::Invalid)?;
            i += 2;
            let c = match escape {
                b'"' => '"',
                b'\\' => '\\',
                b'/' => '/',
                b'b' => '\u{8}',
                b'f' => '\u{c}',
                b'n' => '\n',
                b'r' => '\r',
                b't' => '\t',
                b'u' => {
                    let mut unit = self.hex4(i).ok_or(JsonError::Invalid)?;
                    i += 4;
                    // CPython joins a surrogate pair only when at least one more
                    // character follows the second escape.
                    if (0xd800..0xdc00).contains(&unit) && i + 6 < self.bytes.len() && self.literal(i, "\\u") {
                        let low = self.hex4(i + 2).ok_or(JsonError::Invalid)?;
                        if (0xdc00..0xe000).contains(&low) {
                            unit = 0x10000 + ((unit - 0xd800) << 10) + (low - 0xdc00);
                            i += 6;
                        }
                    }
                    char::from_u32(unit).unwrap_or('\u{fffd}')
                }
                _ => return Err(JsonError::Invalid),
            };
            out.push(c);
        }
    }

    fn digits(&self, mut i: usize) -> usize {
        while matches!(self.at(i), Some(b) if b.is_ascii_digit()) {
            i += 1;
        }
        i
    }

    fn number(&self, start: usize) -> Result<(PyJson, usize), JsonError> {
        let mut i = start;
        if self.at(i) == Some(b'-') {
            i += 1;
        }
        let int_start = i;
        match self.at(i) {
            Some(b'0') => i += 1,
            Some(b) if b.is_ascii_digit() => i = self.digits(i),
            _ => return Err(JsonError::Invalid),
        }
        let int_end = i;
        let mut is_float = false;
        if self.at(i) == Some(b'.') && matches!(self.at(i + 1), Some(b) if b.is_ascii_digit()) {
            is_float = true;
            i = self.digits(i + 1);
        }
        if matches!(self.at(i), Some(b'e') | Some(b'E')) {
            let mut j = i + 1;
            if matches!(self.at(j), Some(b'+') | Some(b'-')) {
                j += 1;
            }
            if matches!(self.at(j), Some(b) if b.is_ascii_digit()) {
                is_float = true;
                i = self.digits(j);
            }
        }
        let text = &self.text[start..i];
        if is_float {
            let value = text.parse::<f64>().map_err(|_| JsonError::Invalid)?;
            return Ok((PyJson::Float(value), i));
        }
        if int_end - int_start > MAX_INT_DIGITS {
            return Err(JsonError::Invalid); // int() refuses: ValueError
        }
        let text = if text == "-0" { "0" } else { text };
        Ok((PyJson::Int(text.to_string()), i))
    }
}
