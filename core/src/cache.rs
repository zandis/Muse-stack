//! What may be answered from the exact-repeat cache, and its key (the pure part
//! of `muse/cache.py`). The store itself (memory + SQLite) is not ported yet.

use sha2::{Digest, Sha256};

use crate::error::PyError;
use crate::pyjson::PyJson;
use crate::pytext;

/// Parameters that change the answer. Anything else (metadata, user, timeouts) does not.
pub const OUTPUT_PARAMS: [&str; 12] = [
    "temperature", "top_p", "max_tokens", "max_completion_tokens", "stop", "seed", "response_format",
    "presence_penalty", "frequency_penalty", "logit_bias", "reasoning_effort", "top_k",
];
/// A request that sets any of these is never cached.
pub const NEVER: [&str; 7] = ["stream", "tools", "functions", "tool_choice", "function_call", "logprobs", "top_logprobs"];

/// Python's `value in (None, False, [], {})`: also true for 0 and 0.0, which equal False.
fn is_unset(value: Option<&PyJson>) -> bool {
    match value {
        None | Some(PyJson::Null) | Some(PyJson::Bool(false)) => true,
        Some(PyJson::Int(text)) => text == "0",
        Some(PyJson::Float(x)) => *x == 0.0,
        Some(PyJson::List(items)) => items.is_empty(),
        Some(PyJson::Dict(items)) => items.is_empty(),
        Some(PyJson::Bool(true)) | Some(PyJson::Str(_)) => false,
    }
}

/// Python's `float(value)`: Ok(None) stands for the TypeError/ValueError the
/// caller catches, Err for the OverflowError it does not.
fn as_float(value: &PyJson) -> Result<Option<f64>, PyError> {
    Ok(match value {
        PyJson::Bool(b) => Some(f64::from(u8::from(*b))),
        PyJson::Int(text) => match text.parse::<f64>() {
            Ok(x) if x.is_finite() => Some(x),
            _ => return Err(PyError::Overflow),
        },
        PyJson::Float(x) => Some(*x),
        PyJson::Str(s) => pytext::py_float(s),
        PyJson::Null | PyJson::List(_) | PyJson::Dict(_) => None,
    })
}

/// (may be cached, why). A chat request with plain messages whose answer should
/// not vary: temperature 0, or temperature unset when the task is `deterministic`;
/// never with a stream, tools, logprobs or n > 1.
///
/// `data` is the request body; anything but an object is treated as empty.
/// Error: `Overflow` for an integer temperature too large for a float.
pub fn cacheable(data: &PyJson, deterministic: bool) -> Result<(bool, String), PyError> {
    if !matches!(data.get("messages"), Some(PyJson::List(items)) if !items.is_empty()) {
        return Ok((false, "not a chat request".to_string()));
    }
    for key in NEVER {
        if !is_unset(data.get(key)) {
            return Ok((false, format!("'{key}' is set")));
        }
    }
    let n_ok = match data.get("n") {
        None | Some(PyJson::Null) | Some(PyJson::Bool(true)) => true,
        Some(PyJson::Int(text)) => text == "1",
        Some(PyJson::Float(x)) => *x == 1.0,
        _ => false,
    };
    if !n_ok {
        return Ok((false, "n > 1".to_string()));
    }
    let temperature = match data.get("temperature") {
        None | Some(PyJson::Null) => {
            return Ok(if deterministic {
                (true, "temperature unset on a deterministic task".to_string())
            } else {
                (false, "temperature is not 0".to_string())
            })
        }
        Some(value) => value,
    };
    Ok(match as_float(temperature)? {
        Some(0.0) => (true, "temperature 0".to_string()),
        Some(_) => (false, "temperature is not 0".to_string()),
        None => (false, "temperature is not a number".to_string()),
    })
}

/// SHA-256 (lower-case hex) over the canonical JSON of what decides the answer
/// and who may see it: the alias, the messages, every [`OUTPUT_PARAMS`] entry
/// that is set, the privacy class and the app's default class.
pub fn cache_key(alias: &str, data: &PyJson, privacy: &str, app_default_privacy: &str) -> String {
    let params: Vec<(String, PyJson)> = OUTPUT_PARAMS
        .iter()
        .filter_map(|key| match data.get(key) {
            None | Some(PyJson::Null) => None,
            Some(value) => Some((key.to_string(), value.clone())),
        })
        .collect();
    let doc = PyJson::Dict(vec![
        ("v".to_string(), PyJson::Int("1".to_string())),
        ("alias".to_string(), PyJson::Str(alias.to_string())),
        ("messages".to_string(), data.get("messages").cloned().unwrap_or(PyJson::Null)),
        ("params".to_string(), PyJson::Dict(params)),
        ("privacy".to_string(), PyJson::Str(privacy.to_string())),
        ("app_privacy".to_string(), PyJson::Str(app_default_privacy.to_string())),
    ]);
    let digest = Sha256::digest(doc.dumps_canonical().as_bytes());
    digest.iter().map(|byte| format!("{byte:02x}")).collect()
}
