//! Turn provider rate-limit signals into blocks (port of `muse/quota.py`).
//!
//! Pure functions over a failed response (status, headers, body): how long an
//! upstream model, or a whole provider, should be left alone. Times are epoch
//! seconds (f64) and `now` is always passed in. Every function here returns what
//! the Python function of the same name returns for the same input; where Python
//! raises, the result is an `Err(PyError)`.

use serde::{Deserialize, Serialize};

use crate::civil::{self, DAY, HOUR};
use crate::error::PyError;
use crate::pyjson::{JsonError, PyJson};
use crate::pytext::{self, fmax, truthy};

/// Never trust a reset further out than this (31 days).
pub const MAX_TRUSTED_SECONDS: f64 = 31.0 * 86400.0;
pub const BAD_KEY_SECONDS: f64 = 15.0 * 60.0;
pub const PAYMENT_SECONDS: f64 = 60.0 * 60.0;
pub const DEFAULT_COOLDOWN_SECONDS: f64 = 60.0;
/// A Retry-After longer than this is a quota, not a blip.
pub const LONG_RETRY_AFTER: f64 = 3600.0;

/// Leave an upstream (or a whole provider) alone until `until`.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Block {
    /// `"upstream"` or `"provider"`.
    pub scope: String,
    /// `"cooldown"` or `"exhausted"`.
    pub kind: String,
    pub until: f64,
    pub reason: String,
}

impl Block {
    /// The ledger scope key: the upstream string, or `provider:<id>`.
    pub fn key(&self, provider_id: &str, upstream: Option<&str>) -> String {
        match upstream {
            Some(upstream) if self.scope != "provider" && !upstream.is_empty() => upstream.to_string(),
            _ => format!("provider:{provider_id}"),
        }
    }
}

// ---------------------------------------------------------------- time helpers

/// `[0-9]+(?:\.[0-9]+)?` over the whole text.
fn is_ascii_number(text: &str) -> bool {
    let (whole, fraction) = match text.split_once('.') {
        Some((whole, fraction)) => (whole, Some(fraction)),
        None => (text, None),
    };
    let digits = |part: &str| !part.is_empty() && part.bytes().all(|b| b.is_ascii_digit());
    digits(whole) && fraction.map(digits).unwrap_or(true)
}

/// The number at the start of `text` (`[0-9]+(?:\.[0-9]+)?`, longest match) and its length.
fn leading_number(text: &str) -> Option<(f64, usize)> {
    let bytes = text.as_bytes();
    let mut end = 0;
    while end < bytes.len() && bytes[end].is_ascii_digit() {
        end += 1;
    }
    if end == 0 {
        return None;
    }
    if bytes.get(end) == Some(&b'.') {
        let mut fraction_end = end + 1;
        while fraction_end < bytes.len() && bytes[fraction_end].is_ascii_digit() {
            fraction_end += 1;
        }
        if fraction_end > end + 1 {
            end = fraction_end;
        }
    }
    text[..end].parse::<f64>().ok().map(|value| (value, end))
}

/// `2m59.56s`, `7.66s`, `1h2m`, `6ms`, `41s` or bare seconds -> seconds.
pub fn parse_duration(value: Option<&str>) -> Option<f64> {
    let text = pytext::strip(value?).to_lowercase();
    if text.is_empty() {
        return None;
    }
    if is_ascii_number(&text) {
        let seconds: f64 = text.parse().ok()?;
        return seconds.is_finite().then_some(seconds);
    }
    // d, h, m (not the m of "ms"), s, ms: each optional, in this order, at least one.
    const UNITS: [(&str, f64); 5] = [("d", 86400.0), ("h", 3600.0), ("m", 60.0), ("s", 1.0), ("ms", 0.001)];
    fn walk(rest: &str, unit: usize, parts: &mut [Option<f64>; 5]) -> bool {
        if unit == UNITS.len() {
            return rest.is_empty();
        }
        if let Some((number, len)) = leading_number(rest) {
            let after = &rest[len..];
            let name = UNITS[unit].0;
            if after.starts_with(name) && !(name == "m" && after[1..].starts_with('s')) {
                parts[unit] = Some(number);
                if walk(&after[name.len()..], unit + 1, parts) {
                    return true;
                }
                parts[unit] = None;
            }
        }
        walk(rest, unit + 1, parts)
    }
    let mut parts = [None; 5];
    if !walk(&text, 0, &mut parts) || parts.iter().all(Option::is_none) {
        return None;
    }
    let part = |i: usize| parts[i].unwrap_or(0.0);
    let seconds = part(0) * 86400.0 + part(1) * 3600.0 + part(2) * 60.0 + part(3) + part(4) / 1000.0;
    seconds.is_finite().then_some(seconds)
}

const MONTH_NAMES: [&str; 24] = [
    "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec", "january", "february",
    "march", "april", "may", "june", "july", "august", "september", "october", "november", "december",
];
const DAY_NAMES: [&str; 7] = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];
const ZONES: [(&str, i128); 14] = [
    ("UT", 0), ("UTC", 0), ("GMT", 0), ("Z", 0), ("AST", -400), ("ADT", -300), ("EST", -500), ("EDT", -400),
    ("CST", -600), ("CDT", -500), ("MST", -700), ("MDT", -600), ("PST", -800), ("PDT", -700),
];

/// `email.utils.parsedate_to_datetime(text).timestamp()`, with a date that has
/// no zone read as UTC (what `parse_retry_after` does). None where Python raises.
///
/// This is the standard library's lenient RFC 2822 reader, step for step:
/// optional day name, `12 Nov 2026` or `Nov 12 2026` or `12-Nov-26`, a time
/// with `:` or `.` separators, two-digit years, named or numeric zones.
fn http_date_timestamp(text: &str) -> Option<f64> {
    let mut data: Vec<String> = pytext::split_ws(text).into_iter().map(str::to_string).collect();
    if data.is_empty() {
        return None;
    }
    if data[0].ends_with(',') || DAY_NAMES.contains(&data[0].to_lowercase().as_str()) {
        data.remove(0);
    } else if let Some(i) = data[0].rfind(',') {
        data[0] = data[0][i + 1..].to_string();
    }
    if data.len() == 3 {
        // RFC 850: 12-Nov-26
        let stuff: Vec<String> = data[0].split('-').map(str::to_string).collect();
        if stuff.len() == 3 {
            data.splice(0..1, stuff);
        }
    }
    if data.len() == 4 {
        let s = data[3].clone();
        match s.find('+').or_else(|| s.find('-')) {
            Some(i) if i > 0 => {
                data[3] = s[..i].to_string();
                data.push(s[i..].to_string());
            }
            _ => data.push(String::new()), // no zone
        }
    }
    if data.len() < 5 {
        return None;
    }
    let (mut dd, mut mm, mut yy, mut tm, mut tz) =
        (data[0].clone(), data[1].clone(), data[2].clone(), data[3].clone(), data[4].clone());
    if dd.is_empty() || mm.is_empty() || yy.is_empty() {
        return None;
    }
    mm = mm.to_lowercase();
    if !MONTH_NAMES.contains(&mm.as_str()) {
        let lowered = dd.to_lowercase();
        dd = std::mem::replace(&mut mm, lowered);
        if !MONTH_NAMES.contains(&mm.as_str()) {
            return None;
        }
    }
    let mut month = MONTH_NAMES.iter().position(|name| *name == mm)? as i64 + 1;
    if month > 12 {
        month -= 12;
    }
    if dd.ends_with(',') {
        dd.pop();
    }
    if matches!(yy.find(':'), Some(i) if i > 0) {
        std::mem::swap(&mut yy, &mut tm);
    }
    if yy.ends_with(',') {
        yy.pop();
        if yy.is_empty() {
            return None;
        }
    }
    if !pytext::is_digit(yy.chars().next()?) {
        std::mem::swap(&mut yy, &mut tz);
    }
    if tm.is_empty() {
        return None; // Python: IndexError, which parse_retry_after catches
    }
    if tm.ends_with(',') {
        tm.pop();
    }
    let mut fields: Vec<&str> = tm.split(':').collect();
    if fields.len() == 1 && fields[0].contains('.') {
        fields = fields[0].split('.').collect();
    } else if fields.len() == 1 {
        return None;
    }
    let (thh, tmm, tss) = match fields.as_slice() {
        [h, m] => (*h, *m, "0"),
        [h, m, s] => (*h, *m, *s),
        _ => return None,
    };
    let mut year = pytext::py_int(&yy)?;
    let day = pytext::py_int(&dd)?;
    let hour = pytext::py_int(thh)?;
    let minute = pytext::py_int(tmm)?;
    let second = pytext::py_int(tss)?;
    if year < 100 {
        year += if year > 68 { 1900 } else { 2000 };
    }
    let tz = tz.to_uppercase();
    let mut offset: Option<i128> = match ZONES.iter().find(|(name, _)| *name == tz) {
        Some((_, value)) => Some(*value),
        None => {
            let parsed = pytext::py_int(&tz);
            if parsed == Some(0) && tz.starts_with('-') { None } else { parsed }
        }
    };
    if let Some(value) = offset.filter(|value| *value != 0) {
        let magnitude = value.saturating_abs();
        let seconds = (magnitude / 100).saturating_mul(3600).saturating_add((magnitude % 100) * 60);
        offset = Some(if value < 0 { -seconds } else { seconds });
    }
    // datetime(...) and timezone(...) validate their arguments.
    if !(1..=9999).contains(&year) || !(0..=23).contains(&hour) || !(0..=59).contains(&minute) || !(0..=59).contains(&second) {
        return None;
    }
    let (year, day) = (year as i64, i64::try_from(day).ok()?);
    if day < 1 || day > civil::days_in_month(year, month) {
        return None;
    }
    let offset = offset.unwrap_or(0);
    if offset.abs() >= 86400 {
        return None;
    }
    let seconds = civil::days_from_civil(year, month, day) * 86400 + (hour * 3600 + minute * 60 + second - offset) as i64;
    Some(seconds as f64)
}

/// Retry-After as seconds from `now`.
///
/// Accepts delta-seconds (ASCII digits, optional fraction) and HTTP-dates.
/// Negative, NaN, non-ASCII digits and garbage give None. Values beyond
/// [`MAX_TRUSTED_SECONDS`] are rejected rather than clamped.
pub fn parse_retry_after(value: Option<&str>, now: f64) -> Option<f64> {
    let text = pytext::strip(value?);
    let first = text.chars().next()?;
    let seconds = if is_ascii_number(text) {
        text.parse::<f64>().ok()?
    } else {
        if first == '+' || first == '-' || pytext::is_digit(first) {
            return None; // "-5", "1e3", "١٢"
        }
        fmax(0.0, http_date_timestamp(text)? - now)
    };
    // Not finite, negative, or further out than is trusted.
    (0.0..=MAX_TRUSTED_SECONDS).contains(&seconds).then_some(seconds)
}

/// A reset header as an absolute epoch time.
///
/// Handles durations (`2m59.56s`), delta seconds, epoch seconds and epoch
/// milliseconds. A value in the past means "now". Anything over 31 days out
/// is not trusted (None).
pub fn parse_reset(value: Option<&str>, now: f64) -> Option<f64> {
    let text = pytext::strip(value?);
    if text.is_empty() {
        return None;
    }
    let at = if is_ascii_number(text) {
        let number: f64 = text.parse().ok()?;
        if number >= 1e12 {
            number / 1000.0 // epoch milliseconds
        } else if number >= 1e9 {
            number // epoch seconds
        } else {
            now + number
        }
    } else {
        now + parse_duration(Some(text))?
    };
    if !at.is_finite() {
        return None;
    }
    let at = fmax(at, now);
    (at - now <= MAX_TRUSTED_SECONDS).then_some(at)
}

/// US Pacific offset in hours by the post-2007 rule, applied to every year as
/// the Python does: PDT from the 2nd Sunday of March 10:00 UTC to the 1st
/// Sunday of November 09:00 UTC.
fn pacific_offset_hours(utc: i64) -> Result<i64, PyError> {
    let (year, _, _) = civil::date_of(utc);
    let sunday_from = |month: i64, day: i64| -> Result<i64, PyError> {
        let start = civil::midnight(year, month, day)?;
        Ok(start + (6 - civil::weekday(start.div_euclid(DAY))).rem_euclid(7) * DAY)
    };
    let dst_start = sunday_from(3, 8)? + 10 * HOUR;
    let dst_end = sunday_from(11, 1)? + 9 * HOUR;
    Ok(if dst_start <= utc && utc < dst_end { -7 } else { -8 })
}

/// Gemini's RPD reset: the next midnight in US Pacific time.
pub fn next_midnight_pacific(now: f64) -> Result<f64, PyError> {
    let utc = civil::from_timestamp(now)?;
    let offset = pacific_offset_hours(utc)?;
    let local = civil::checked(utc + offset * HOUR)?;
    let (year, month, day) = civil::date_of(local);
    let midnight_local = civil::checked(civil::midnight(year, month, day)? + DAY)?;
    let mut candidate = civil::checked(midnight_local - offset * HOUR)?;
    // The offset may differ at the target midnight (DST switch day); recompute once.
    let offset2 = pacific_offset_hours(candidate)?;
    if offset2 != offset {
        candidate = civil::checked(midnight_local - offset2 * HOUR)?;
    }
    Ok(civil::timestamp(candidate))
}

pub fn next_midnight_utc(now: f64) -> Result<f64, PyError> {
    let (year, month, day) = civil::date_of(civil::from_timestamp(now)?);
    Ok(civil::timestamp(civil::checked(civil::midnight(year, month, day)? + DAY)?))
}

pub fn next_month_utc(now: f64) -> Result<f64, PyError> {
    let (year, month, _) = civil::date_of(civil::from_timestamp(now)?);
    let (year, month) = if month == 12 { (year + 1, 1) } else { (year, month + 1) };
    Ok(civil::timestamp(civil::midnight(year, month, 1)?))
}

/// Start of the provider's quota day: Pacific for Gemini, UTC otherwise.
/// (For Gemini this is the next Pacific midnight minus 24 h, as in Python; on
/// the two DST switch days that is an hour off the real local midnight. Use
/// `ration::previous_midnight_pacific` for the exact one.)
pub fn day_start(now: f64, provider_id: Option<&str>) -> Result<f64, PyError> {
    if provider_id == Some("gemini") {
        return Ok(next_midnight_pacific(now)? - 86400.0);
    }
    Ok(next_midnight_utc(now)? - 86400.0)
}

pub fn month_start(now: f64) -> Result<f64, PyError> {
    let (year, month, _) = civil::date_of(civil::from_timestamp(now)?);
    Ok(civil::timestamp(civil::midnight(year, month, 1)?))
}

fn daily_reset(provider_id: &str, now: f64) -> Result<f64, PyError> {
    if provider_id == "gemini" { next_midnight_pacific(now) } else { next_midnight_utc(now) }
}

/// ISO-8601 UTC with a trailing Z (`2026-11-12T07:00:00Z`), the snapshot's time
/// format. Fractions of a second are dropped after rounding to the microsecond.
pub fn iso(ts: Option<f64>) -> Result<Option<String>, PyError> {
    let Some(ts) = ts else { return Ok(None) };
    let micros = civil::from_timestamp(ts)?;
    let (year, month, day) = civil::date_of(micros);
    let second_of_day = micros.rem_euclid(DAY) / civil::SECOND;
    // `%Y` is not zero-padded by glibc, which is what the Python runs on.
    Ok(Some(format!(
        "{year}-{month:02}-{day:02}T{:02}:{:02}:{:02}Z",
        second_of_day / 3600,
        second_of_day / 60 % 60,
        second_of_day % 60
    )))
}

// ---------------------------------------------------------------- header/body helpers

/// Header names and values in arrival order. A value of None is skipped, as a
/// Python `None` value is.
pub type HeaderPairs<'a> = &'a [(String, Option<String>)];

/// A small insertion-ordered map: what a Python dict of headers is.
#[derive(Debug, Clone, Default, PartialEq)]
pub struct Headers(Vec<(String, String)>);

impl Headers {
    pub fn get(&self, name: &str) -> Option<&str> {
        self.0.iter().find(|(k, _)| k == name).map(|(_, v)| v.as_str())
    }

    fn set(&mut self, name: String, value: String) {
        match self.0.iter_mut().find(|(k, _)| *k == name) {
            Some(slot) => slot.1 = value,
            None => self.0.push((name, value)),
        }
    }

    pub fn iter(&self) -> impl Iterator<Item = (&str, &str)> {
        self.0.iter().map(|(k, v)| (k.as_str(), v.as_str()))
    }
}

fn normalise<I: IntoIterator<Item = (String, Option<String>)>>(pairs: I) -> Headers {
    let mut out = Headers::default();
    let mut prefixed = Headers::default();
    for (key, value) in pairs {
        let Some(value) = value else { continue };
        let name = pytext::strip(&key).to_lowercase();
        match name.strip_prefix("llm_provider-") {
            Some(rest) => prefixed.set(rest.to_string(), value),
            None => out.set(name, value),
        }
    }
    for (name, value) in prefixed.0 {
        out.set(name, value);
    }
    out
}

/// Lower-case names; strip LiteLLM's `llm_provider-` prefix (the raw upstream
/// copy wins over LiteLLM's re-exported one). A later duplicate name wins.
pub fn normalise_headers(headers: Option<HeaderPairs<'_>>) -> Headers {
    normalise(headers.unwrap_or(&[]).iter().cloned())
}

/// Every JSON object embedded in the text (LiteLLM often prefixes a message).
fn json_candidates(body: &str) -> Result<Vec<PyJson>, PyError> {
    let mut found = Vec::new();
    let mut index = body.find('{');
    let mut scanned = 0;
    while let Some(at) = index {
        if scanned >= 20 {
            break;
        }
        scanned += 1;
        match PyJson::raw_decode(body, at) {
            Ok((value, end)) => {
                found.push(value);
                index = body[end..].find('{').map(|i| end + i);
            }
            Err(JsonError::Invalid) => index = body[at + 1..].find('{').map(|i| at + 1 + i),
            Err(JsonError::TooDeep) => return Err(PyError::Recursion),
        }
    }
    Ok(found)
}

/// Every object inside a JSON value: at most 8 levels down, 50 items per array.
fn walk<'a>(value: &'a PyJson, depth: usize, out: &mut Vec<&'a [(String, PyJson)]>) {
    if depth > 8 {
        return;
    }
    match value {
        PyJson::Dict(items) => {
            out.push(items);
            for (_, item) in items {
                walk(item, depth + 1, out);
            }
        }
        PyJson::List(items) => {
            for item in items.iter().take(50) {
                walk(item, depth + 1, out);
            }
        }
        _ => {}
    }
}

struct Signals {
    text: String, // lower-cased body
    gemini_daily: bool,
    gemini_minute: bool,
    retry_delay: Option<f64>,         // Gemini RetryInfo
    embedded_headers: Option<Headers>, // OpenRouter error.metadata.headers
}

fn is_quote(c: char) -> bool {
    c == '\'' || c == '"'
}

/// After a key name: `['"]?\s*:\s*['"]`, returning what follows the opening quote.
fn after_key_colon_quote(rest: &str) -> Option<&str> {
    let rest = rest.strip_prefix(is_quote).unwrap_or(rest);
    let rest = rest.trim_start_matches(pytext::is_space).strip_prefix(':')?;
    rest.trim_start_matches(pytext::is_space).strip_prefix(is_quote)
}

/// `quotaid['"]?\s*:\s*['"][^'"]*<word>` anywhere in the text.
fn quota_id_mentions(text: &str, word: &str) -> bool {
    text.match_indices("quotaid").any(|(at, key)| {
        after_key_colon_quote(&text[at + key.len()..])
            .map(|value| value[..value.find(is_quote).unwrap_or(value.len())].contains(word))
            .unwrap_or(false)
    })
}

/// `retrydelay['"]?\s*:\s*['"]([0-9.]+s)['"]`: the first match's group.
fn retry_delay_text(text: &str) -> Option<&str> {
    text.match_indices("retrydelay").find_map(|(at, key)| {
        let value = after_key_colon_quote(&text[at + key.len()..])?;
        let run = value.bytes().take_while(|b| b.is_ascii_digit() || *b == b'.').count();
        let after = &value[run..];
        (run > 0 && after.starts_with('s') && after[1..].starts_with(is_quote)).then(|| &value[..run + 1])
    })
}

/// `(?:please )?try again in ([0-9hms.]+)`: the first match's group.
fn try_again_text(text: &str) -> Option<&str> {
    text.match_indices("try again in ").find_map(|(at, key)| {
        let value = &text[at + key.len()..];
        let run = value.bytes().take_while(|b| b.is_ascii_digit() || b"hms.".contains(b)).count();
        (run > 0).then(|| &value[..run])
    })
}

fn names_daily_quota(text: &str) -> bool {
    ["per day", "per-day", "(tpd)", "(rpd)", "daily limit", "daily quota"].iter().any(|p| text.contains(p))
}

fn names_monthly_quota(text: &str) -> bool {
    ["/month", "/ month", "per month", "monthly limit", "monthly quota", "calls a month"].iter().any(|p| text.contains(p))
}

fn signals(body: &str) -> Result<Signals, PyError> {
    let mut sig = Signals {
        text: body.to_lowercase(),
        gemini_daily: false,
        gemini_minute: false,
        retry_delay: None,
        embedded_headers: None,
    };
    for doc in json_candidates(body)? {
        let mut nodes = Vec::new();
        walk(&doc, 0, &mut nodes);
        for node in nodes {
            let get = |key: &str| node.iter().find(|(k, _)| k == key).map(|(_, v)| v);
            let mentions = |word: &str| {
                ["quotaId", "quotaMetric"].iter().any(|key| get(key).map(|v| v.str_lower_contains(word)).unwrap_or(false))
            };
            if mentions("perday") {
                sig.gemini_daily = true;
            }
            if mentions("perminute") {
                sig.gemini_minute = true;
            }
            if let Some(delay) = get("retryDelay") {
                sig.retry_delay = match delay {
                    PyJson::Null => None,
                    other => parse_duration(Some(&other.scalar_str())),
                };
            }
            if let Some(PyJson::Dict(headers)) = get("headers") {
                if headers.iter().any(|(k, _)| k.to_lowercase().contains("ratelimit")) {
                    sig.embedded_headers = Some(normalise(headers.iter().map(|(k, v)| {
                        (k.clone(), (!matches!(v, PyJson::Null)).then(|| v.scalar_str()))
                    })));
                }
            }
        }
    }
    // Gemini via LiteLLM's gemini/ provider can arrive as Python-repr text, not JSON.
    if !sig.gemini_daily && quota_id_mentions(&sig.text, "perday") {
        sig.gemini_daily = true;
    }
    if !sig.gemini_minute && quota_id_mentions(&sig.text, "perminute") {
        sig.gemini_minute = true;
    }
    if sig.retry_delay.is_none() {
        if let Some(delay) = retry_delay_text(&sig.text) {
            sig.retry_delay = parse_duration(Some(delay));
        }
    }
    Ok(sig)
}

fn block(scope: &str, kind: &str, until: f64, reason: impl Into<String>, now: f64) -> Block {
    Block { scope: scope.to_string(), kind: kind.to_string(), until: fmax(until, now), reason: reason.into() }
}

fn trusted(at: Option<f64>, now: f64) -> Option<f64> {
    let at = at?;
    if !at.is_finite() || at - now > MAX_TRUSTED_SECONDS {
        return None;
    }
    Some(fmax(at, now))
}

// ---------------------------------------------------------------- classify

/// Decide whether a failed call should block its upstream or provider.
///
/// `Ok(None)` means "leave it to LiteLLM": caller errors (400/404/413/422),
/// 5xx, a missing status, and anything not understood. `status` is None for
/// anything that is not an integer status code.
///
/// Errors: `now` outside the years 1..=9999 when a calendar reset is needed,
/// and a body holding JSON nested deeper than [`crate::pyjson::MAX_DEPTH`].
pub fn classify_failure(
    provider_id: &str,
    status: Option<i64>,
    headers: Option<HeaderPairs<'_>>,
    body_text: Option<&str>,
    now: f64,
) -> Result<Option<Block>, PyError> {
    let provider_id = provider_id.to_lowercase();
    let Some(status) = status else { return Ok(None) };
    let hdrs = normalise_headers(headers);
    let sig = signals(body_text.unwrap_or(""))?;
    let text = sig.text.as_str();

    if status == 401 || status == 403 {
        let what = if status == 401 { "bad or revoked key" } else { "key has no access (403)" };
        return Ok(Some(block(
            "provider", "exhausted", now + BAD_KEY_SECONDS,
            format!("{what} for {provider_id}: check the key; retrying in 15 min"), now,
        )));
    }
    if status == 402 {
        return Ok(Some(block(
            "provider", "exhausted", now + PAYMENT_SECONDS,
            format!("{provider_id} says payment required (out of credit); retrying in 1 h"), now,
        )));
    }
    if status != 429 {
        return Ok(None); // caller errors, 5xx, 408, anything else
    }

    let retry_after = parse_retry_after(hdrs.get("retry-after"), now);

    // --- Gemini: RESOURCE_EXHAUSTED with QuotaFailure details.
    if sig.gemini_daily {
        // retryDelay on a per-day violation is known to be a short, wrong hint.
        let gemini = provider_id == "gemini";
        return Ok(Some(block(
            "upstream", "exhausted",
            if gemini { next_midnight_pacific(now)? } else { next_midnight_utc(now)? },
            if gemini {
                "daily quota exhausted (quotaId PerDay); resets at midnight Pacific (estimated)"
            } else {
                "daily quota exhausted (PerDay); resets at UTC midnight (estimated)"
            },
            now,
        )));
    }
    if sig.gemini_minute {
        let (wait, source) = match (sig.retry_delay, retry_after) {
            (Some(delay), _) => (delay, "retryDelay"),
            (None, Some(after)) => (after, "Retry-After"),
            (None, None) => (DEFAULT_COOLDOWN_SECONDS, "estimated"),
        };
        return Ok(Some(block(
            "upstream", "cooldown", now + pytext::fmin(wait, LONG_RETRY_AFTER),
            format!("per-minute quota (quotaId PerMinute); {source}"), now,
        )));
    }

    // --- OpenRouter: account-wide limits on :free models.
    if provider_id == "openrouter" && (text.contains("free-models-per-day") || text.contains("free-models-per-min")) {
        let from_header = hdrs.get("x-ratelimit-reset").filter(|value| !value.is_empty());
        let embedded = sig.embedded_headers.as_ref().and_then(|h| h.get("x-ratelimit-reset"));
        let reset = parse_reset(from_header.or(embedded), now);
        if text.contains("free-models-per-day") {
            return Ok(Some(match reset {
                Some(at) if trusted(reset, now).is_some() && at > now => {
                    block("provider", "exhausted", at, "OpenRouter free-models-per-day limit; X-RateLimit-Reset", now)
                }
                _ => block(
                    "provider", "exhausted", next_midnight_utc(now)?,
                    "OpenRouter free-models-per-day limit; resets at UTC midnight (estimated)", now,
                ),
            }));
        }
        let wait = match retry_after {
            Some(after) => Some(after),
            None if truthy(reset) => reset.map(|at| at - now),
            None => None,
        };
        let wait = if truthy(wait) { wait.unwrap_or(DEFAULT_COOLDOWN_SECONDS) } else { DEFAULT_COOLDOWN_SECONDS };
        return Ok(Some(block("provider", "cooldown", now + wait, "OpenRouter free-models-per-minute limit", now)));
    }

    // --- Cohere trial monthly cap.
    if provider_id == "cohere" && text.contains("trial") && names_monthly_quota(text) {
        return Ok(Some(block(
            "provider", "exhausted", next_month_utc(now)?,
            "Cohere trial monthly call limit; resets on the 1st (UTC, estimated)", now,
        )));
    }

    // --- Text that names a daily or monthly quota (Groq TPD/RPD, generic).
    let hint = try_again_text(text).and_then(|found| parse_duration(Some(found.trim_end_matches('.'))));
    if names_daily_quota(text) {
        // Groq's TPD/RPD are rolling windows, so its "try again in" hint is the real wait.
        let latest = match (hint.map(|h| now + h), retry_after.map(|r| now + r)) {
            (Some(a), Some(b)) => Some(fmax(a, b)),
            (a, b) => a.or(b),
        };
        return Ok(Some(match trusted(latest, now) {
            Some(until) if until > now => {
                block("upstream", "exhausted", until, "daily quota exhausted; provider retry hint", now)
            }
            _ => block(
                "upstream", "exhausted", daily_reset(&provider_id, now)?,
                format!(
                    "daily quota exhausted; reset time estimated {}",
                    if provider_id == "gemini" { "(midnight Pacific)" } else { "(UTC midnight)" }
                ),
                now,
            ),
        }));
    }
    if names_monthly_quota(text) && (text.contains("limit") || text.contains("quota")) {
        return Ok(Some(block(
            "provider", "exhausted", next_month_utc(now)?,
            "monthly quota exhausted; resets on the 1st (UTC, estimated)", now,
        )));
    }

    // --- Short rate limit.
    if let Some(after) = retry_after {
        return Ok(Some(if after > LONG_RETRY_AFTER {
            block("upstream", "exhausted", now + after, "429 with a long Retry-After", now)
        } else {
            block("upstream", "cooldown", now + after, "429 rate limit; Retry-After", now)
        }));
    }
    if let Some(hint) = hint.filter(|h| *h <= MAX_TRUSTED_SECONDS) {
        let kind = if hint > LONG_RETRY_AFTER { "exhausted" } else { "cooldown" };
        return Ok(Some(block("upstream", kind, now + hint, "429 rate limit; provider retry hint", now)));
    }
    let latest_reset = hdrs
        .iter()
        .filter(|(name, _)| name.starts_with("x-ratelimit-reset") || name.starts_with("ratelimit-reset"))
        .filter_map(|(_, value)| parse_reset(Some(value), now))
        .filter(|at| *at > now)
        .reduce(fmax);
    if let Some(until) = latest_reset {
        let kind = if until - now > LONG_RETRY_AFTER { "exhausted" } else { "cooldown" };
        return Ok(Some(block("upstream", kind, until, "429 rate limit; x-ratelimit-reset", now)));
    }
    // "quota" or "limit" as a whole word, and no clock above: a daily cap
    // whose wording matched nothing else was retried every minute. A bare
    // 429 (neither word) stays at 60s.
    if names_quota_or_limit(text) {
        return Ok(Some(block(
            "upstream",
            "exhausted",
            daily_reset(&provider_id, now)?,
            format!(
                "429 mentions quota or limit but names no clock; reset time estimated {}",
                if provider_id == "gemini" { "(midnight Pacific)" } else { "(UTC midnight)" }
            ),
            now,
        )));
    }
    Ok(Some(block(
        "upstream", "cooldown", now + DEFAULT_COOLDOWN_SECONDS,
        "429 rate limit; no reset given (estimated 60 s)", now,
    )))
}

/// Python `\b(?:quota|limit)\b` on the already-lowercased body. ASCII words
/// only: `quotaId` and `unlimited` do not match.
fn names_quota_or_limit(text: &str) -> bool {
    fn word(text: &str, needle: &str) -> bool {
        let bytes = text.as_bytes();
        let needle = needle.as_bytes();
        let mut i = 0;
        while i + needle.len() <= bytes.len() {
            if &bytes[i..i + needle.len()] == needle {
                let before = i == 0 || !is_word_byte(bytes[i - 1]);
                let after = i + needle.len() == bytes.len() || !is_word_byte(bytes[i + needle.len()]);
                if before && after {
                    return true;
                }
            }
            i += 1;
        }
        false
    }
    fn is_word_byte(b: u8) -> bool {
        b.is_ascii_alphanumeric() || b == b'_'
    }
    word(text, "quota") || (word(text, "limit") && !names_a_short_limit(text))
}

/// Python `\brate[ _-]?limit|too many requests|per[ -]minute|\b[rt]pm\b` on the
/// already-lowercased body: the wording of a per-minute limit. A 429 that says
/// this and names no clock waits 60 seconds, not a day.
fn names_a_short_limit(text: &str) -> bool {
    fn is_word_byte(b: u8) -> bool {
        b.is_ascii_alphanumeric() || b == b'_'
    }
    // `needle` with a word boundary before it (and after it, when `closed`).
    fn at_boundary(text: &str, needle: &str, closed: bool) -> bool {
        let bytes = text.as_bytes();
        let n = needle.as_bytes();
        let mut i = 0;
        while i + n.len() <= bytes.len() {
            if &bytes[i..i + n.len()] == n {
                let before = i == 0 || !is_word_byte(bytes[i - 1]);
                let after = !closed || i + n.len() == bytes.len() || !is_word_byte(bytes[i + n.len()]);
                if before && after {
                    return true;
                }
            }
            i += 1;
        }
        false
    }
    ["ratelimit", "rate limit", "rate_limit", "rate-limit"].iter().any(|n| at_boundary(text, n, false))
        || text.contains("too many requests")
        || text.contains("per minute")
        || text.contains("per-minute")
        || at_boundary(text, "rpm", true)
        || at_boundary(text, "tpm", true)
}
