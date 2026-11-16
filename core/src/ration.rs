//! Pacing and the user-facing options trailer (the pure part of `muse/ration.py`).
//!
//! Ported in this slice: the window arithmetic (`window_bounds`, `window_reset`,
//! `previous_midnight_pacific`), the pacing line (`burn_rate`, `pace_position`,
//! `pace_release_at`, `pace_holds`) and `options_trailer` / `parse_options`.
//! The rules themselves (`ration`, `decide`, `prefer`) are not here yet.

use serde::{Deserialize, Serialize};

use crate::error::PyError;
use crate::pytext::{self, fmax, fmin};
use crate::quota;

/// Window lengths used when only a reset time is known (headers).
fn nominal_window_s(window: &str) -> Option<f64> {
    match window {
        "minute" => Some(60.0),
        "day" => Some(86400.0),
        "month" => Some(30.0 * 86400.0),
        _ => None,
    }
}

fn is_paced_window(window: &str) -> bool {
    window == "day" || window == "month"
}

/// The ration settings the pacing functions read. Field names and defaults are
/// those of Python's `ration.Settings`; `paid_cap_usd` (a Decimal), `tz` and
/// `warnings` belong to the rules that are not ported yet.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
#[serde(default)]
pub struct Settings {
    pub active_window_s: f64,
    pub reserve_background: f64,
    pub reserve_normal: f64,
    pub expiring_day_s: f64,
    pub expiring_month_s: f64,
    pub local_yields: bool,
    pub tz_name: String,
    pub pace_margin: f64,
    pub prefer_gap: f64,
    pub burn_window_s: f64,
    pub burn_min_calls: i64,
}

impl Default for Settings {
    fn default() -> Self {
        Settings {
            active_window_s: 120.0,
            reserve_background: 0.30,
            reserve_normal: 0.10,
            expiring_day_s: 3600.0,
            expiring_month_s: 86400.0,
            local_yields: true,
            tz_name: String::new(),
            pace_margin: 0.0,
            prefer_gap: 0.10,
            burn_window_s: 3600.0,
            burn_min_calls: 3,
        }
    }
}

/// A quota whose remaining amount is known for one upstream.
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Limit {
    /// `minute`, `day` or `month`.
    pub window: String,
    /// `requests` or `tokens`.
    pub metric: String,
    pub limit: i64,
    pub remaining: i64,
    pub reset_at: Option<f64>,
    /// `header` or `declared`.
    pub source: String,
    /// The previous reset; None = window length unknown (no pacing).
    #[serde(default)]
    pub window_start: Option<f64>,
    /// Ledger scope it was counted in; "" = the upstream.
    #[serde(default)]
    pub scope: String,
}

/// Where one allowance stands against its straight line. Fractions are of the limit.
///
/// surplus = remaining_fraction - expected_remaining_fraction; > 0 means under
/// the line (use is behind schedule). projected_leftover is the fraction left at
/// reset if the recent burn rate continues (None when the rate is unknown).
#[derive(Debug, Clone, PartialEq, Serialize, Deserialize)]
pub struct Pace {
    pub applies: bool,
    pub reason: String,
    pub remaining_fraction: f64,
    pub expected_remaining_fraction: Option<f64>,
    pub surplus: Option<f64>,
    pub projected_leftover: Option<f64>,
    pub window_start: Option<f64>,
    pub reset_at: Option<f64>,
}

/// When a declared quota window resets: the next UTC minute, the provider's
/// quota day (Pacific for Gemini, else UTC), or, for any other window name,
/// the next UTC month.
pub fn window_reset(window: &str, provider_id: &str, now: f64) -> Result<f64, PyError> {
    match window {
        "minute" => Ok(now - pytext::fmod(now, 60.0) + 60.0),
        "day" => Ok(quota::day_start(now, Some(provider_id))? + 86400.0),
        _ => quota::next_month_utc(now),
    }
}

/// Start of the Pacific calendar day containing `now` (Gemini's last reset).
/// Exact on DST switch days (23 or 25 hours long).
pub fn previous_midnight_pacific(now: f64) -> Result<f64, PyError> {
    quota::next_midnight_pacific(quota::next_midnight_pacific(now)? - 26.0 * 3600.0)
}

/// (previous reset, next reset) of the window containing `now`, or None if unknown.
///
/// Declared (catalogue) windows, `observed = false`: minute = the UTC minute,
/// day = midnight Pacific for Gemini and UTC midnight otherwise, month = UTC
/// calendar month; `reset_at` is ignored. Observed windows (headers) know only
/// the next reset, so the start is reset - the window's nominal length, except a
/// month window whose reset is a UTC month start, which is that calendar month.
/// An observed reset further away than one window, or in the past, is unknown.
pub fn window_bounds(
    window: &str,
    provider_id: &str,
    now: f64,
    reset_at: Option<f64>,
    observed: bool,
) -> Result<Option<(f64, f64)>, PyError> {
    if !observed {
        return Ok(match window {
            "minute" => {
                let start = now - pytext::fmod(now, 60.0);
                Some((start, start + 60.0))
            }
            "day" if provider_id == "gemini" => Some((previous_midnight_pacific(now)?, quota::next_midnight_pacific(now)?)),
            "day" => {
                let next = quota::next_midnight_utc(now)?;
                Some((next - 86400.0, next))
            }
            "month" => Some((quota::month_start(now)?, quota::next_month_utc(now)?)),
            _ => None,
        });
    }
    let (Some(length), Some(reset_at)) = (nominal_window_s(window), reset_at) else { return Ok(None) };
    if !reset_at.is_finite() || reset_at < now {
        return Ok(None);
    }
    if window == "month" && quota::month_start(reset_at)? == reset_at {
        let start = quota::month_start(reset_at - 1.0)?;
        return Ok((start <= now).then_some((start, reset_at)));
    }
    if reset_at - now > length {
        return Ok(None);
    }
    Ok(Some((reset_at - length, reset_at)))
}

/// Units per second over the burn window; None (unknown) with fewer than
/// `min_calls` calls (at least one), an unknown count, or a window of 0 or less.
pub fn burn_rate(units: Option<f64>, calls: Option<i64>, window_s: f64, min_calls: i64) -> Option<f64> {
    let (units, calls) = (units?, calls?);
    if window_s <= 0.0 || calls < min_calls.max(1) {
        return None;
    }
    Some(fmax(0.0, units) / window_s)
}

fn remaining_fraction(limit: &Limit) -> f64 {
    fmin(1.0, fmax(0.0, limit.remaining as f64 / limit.limit as f64))
}

/// The pacing line for one limit at `now`.
///
/// expected_remaining_fraction = 1 - clamp((now - start) / (reset - start), 0, 1)
/// surplus = remaining / limit - expected_remaining_fraction
/// projected_leftover = clamp((remaining - rate * (reset - now)) / limit, 0, 1)
/// Minute windows are rate limits, not allowances, and are never paced.
///
/// Error: `ZeroDivision` for a paced limit of 0 when a `rate` is given.
pub fn pace_position(limit: &Limit, now: f64, rate: Option<f64>) -> Result<Pace, PyError> {
    let rf = if limit.limit > 0 { remaining_fraction(limit) } else { 0.0 };
    let not_paced = |reason: String| Pace {
        applies: false,
        reason,
        remaining_fraction: rf,
        expected_remaining_fraction: None,
        surplus: None,
        projected_leftover: None,
        window_start: None,
        reset_at: None,
    };
    if !is_paced_window(&limit.window) {
        return Ok(not_paced(format!("{} windows are rate limits, not allowances; pacing does not apply", limit.window)));
    }
    let unknown_length =
        || format!("the {} window's length is unknown; pacing does not apply (reserve rule only)", limit.window);
    let (Some(start), Some(reset)) = (limit.window_start, limit.reset_at) else {
        return Ok(not_paced(unknown_length()));
    };
    if reset <= start {
        return Ok(not_paced(unknown_length()));
    }
    let span = reset - start;
    let elapsed = fmin(1.0, fmax(0.0, (now - start) / span));
    let expected = 1.0 - elapsed;
    let leftover = match rate {
        Some(rate) => {
            if limit.limit == 0 {
                return Err(PyError::ZeroDivision);
            }
            Some(fmin(1.0, fmax(0.0, (limit.remaining as f64 - rate * fmax(0.0, reset - now)) / limit.limit as f64)))
        }
        None => None,
    };
    Ok(Pace {
        applies: true,
        reason: String::new(),
        remaining_fraction: rf,
        expected_remaining_fraction: Some(expected),
        surplus: Some(rf - expected),
        projected_leftover: leftover,
        window_start: Some(start),
        reset_at: Some(reset),
    })
}

/// When a held background app may next use this limit if nobody spends it:
/// the line catches up (start + (1 - remaining_fraction + margin) * span), or the
/// expiring horizon opens, whichever is first; never before `now` + 1 s.
///
/// Errors: `Assertion` without a window start or reset, `ZeroDivision` for a limit of 0.
pub fn pace_release_at(limit: &Limit, margin: f64, settings: &Settings, now: f64) -> Result<f64, PyError> {
    let (Some(start), Some(reset)) = (limit.window_start, limit.reset_at) else { return Err(PyError::Assertion) };
    let span = reset - start;
    if limit.limit == 0 {
        return Err(PyError::ZeroDivision);
    }
    let rf = remaining_fraction(limit);
    let caught_up = start + (1.0 - rf + margin) * span;
    let horizon = if limit.window == "day" { settings.expiring_day_s } else { settings.expiring_month_s };
    Ok(fmax(now + 1.0, fmin(fmin(caught_up, reset - horizon), reset)))
}

/// True if background use must wait for this limit: pacing applies and surplus < margin.
pub fn pace_holds(limit: &Limit, settings: &Settings, now: f64) -> bool {
    match pace_position(limit, now, None) {
        Ok(Pace { applies: true, surplus: Some(surplus), .. }) => surplus < settings.pace_margin - 1e-12,
        _ => false,
    }
}

// ---------------------------------------------------------------- options for the user

/// The machine-readable choices a bot turns into the three-choice card.
///
/// Grammar: `options=` then a comma-separated list in this fixed order, only
/// the choices available now: `wait:<ISO-8601 UTC, ...Z>` (the soonest reset),
/// `handoff` (open the user's own subscription app), `pay:<USD, 6 decimals>`
/// (estimated cost). `options=none` when nothing is available.
pub fn options_trailer(wait_at: Option<f64>, handoff: bool, pay_usd: Option<f64>) -> Result<String, PyError> {
    let mut items = Vec::new();
    if let Some(at) = quota::iso(wait_at)? {
        items.push(format!("wait:{at}"));
    }
    if handoff {
        items.push("handoff".to_string());
    }
    if let Some(pay) = pay_usd {
        items.push(if pay.is_nan() { "pay:nan".to_string() } else { format!("pay:{pay:.6}") });
    }
    Ok(format!("options={}", if items.is_empty() { "none".to_string() } else { items.join(",") }))
}

/// What `parse_options` found. Python returns a dict holding only the keys that
/// were present: `wait` (the ISO text, unparsed), `pay`, and `handoff: True`.
#[derive(Debug, Clone, PartialEq, Default, Serialize, Deserialize)]
pub struct Options {
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub wait: Option<String>,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub pay: Option<f64>,
    #[serde(default, skip_serializing_if = "std::ops::Not::not")]
    pub handoff: bool,
}

/// Inverse of [`options_trailer`] for a message that ends with ` | options=...`.
/// A message without a trailer gives the empty `Options`.
///
/// Error: `Value` when a `pay:` amount is not a number.
pub fn parse_options(message: &str) -> Result<Options, PyError> {
    const MARK: &str = " | options=";
    let mut out = Options::default();
    let Some(at) = message.rfind(MARK) else { return Ok(out) };
    for item in pytext::strip(&message[at + MARK.len()..]).split(',') {
        let (name, value) = item.split_once(':').unwrap_or((item, ""));
        match name {
            "wait" => out.wait = Some(value.to_string()),
            "pay" => out.pay = Some(pytext::py_float(value).ok_or(PyError::Value)?),
            "handoff" => out.handoff = true,
            _ => {}
        }
    }
    Ok(out)
}
