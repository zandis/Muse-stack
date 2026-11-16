//! Calendar arithmetic for `datetime` in UTC, in whole microseconds since the epoch.
//!
//! Python's `datetime.fromtimestamp(ts, tz=timezone.utc)` rounds to the nearest
//! microsecond (ties to even) and refuses anything outside the years 1..=9999;
//! day and month boundaries are computed from that rounded value, so the same
//! steps are taken here.

use crate::error::PyError;

pub(crate) const SECOND: i64 = 1_000_000;
pub(crate) const HOUR: i64 = 3600 * SECOND;
pub(crate) const DAY: i64 = 86_400 * SECOND;

/// 0001-01-01T00:00:00Z and 9999-12-31T23:59:59Z in epoch seconds.
const MIN_SECONDS: i64 = -62_135_596_800;
const MAX_SECONDS: i64 = 253_402_300_799;

/// `datetime.fromtimestamp(ts, tz=timezone.utc)` as microseconds since the epoch.
pub(crate) fn from_timestamp(ts: f64) -> Result<i64, PyError> {
    if !ts.is_finite() {
        return Err(PyError::TimeOutOfRange);
    }
    let mut whole = ts.trunc();
    let mut micros = ((ts - whole) * 1e6).round_ties_even();
    if micros >= 1e6 {
        micros -= 1e6;
        whole += 1.0;
    } else if micros < 0.0 {
        micros += 1e6;
        whole -= 1.0;
    }
    if !(MIN_SECONDS as f64..=MAX_SECONDS as f64).contains(&whole) {
        return Err(PyError::TimeOutOfRange);
    }
    Ok(whole as i64 * SECOND + micros as i64)
}

/// A datetime must stay inside the years 1..=9999 (else Python raises OverflowError).
pub(crate) fn checked(micros: i64) -> Result<i64, PyError> {
    if (MIN_SECONDS * SECOND..(MAX_SECONDS + 1) * SECOND).contains(&micros) {
        Ok(micros)
    } else {
        Err(PyError::TimeOutOfRange)
    }
}

/// `datetime.timestamp()` for a value on a whole second.
pub(crate) fn timestamp(micros: i64) -> f64 {
    micros.div_euclid(SECOND) as f64 + micros.rem_euclid(SECOND) as f64 / 1e6
}

/// Days since 1970-01-01 for a proleptic Gregorian date.
pub(crate) fn days_from_civil(year: i64, month: i64, day: i64) -> i64 {
    let y = if month <= 2 { year - 1 } else { year };
    let era = y.div_euclid(400);
    let yoe = y - era * 400;
    let doy = (153 * (if month > 2 { month - 3 } else { month + 9 }) + 2) / 5 + day - 1;
    let doe = yoe * 365 + yoe / 4 - yoe / 100 + doy;
    era * 146_097 + doe - 719_468
}

/// (year, month, day) for days since 1970-01-01.
pub(crate) fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let z = days + 719_468;
    let era = z.div_euclid(146_097);
    let doe = z - era * 146_097;
    let yoe = (doe - doe / 1460 + doe / 36_524 - doe / 146_096) / 365;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let day = doy - (153 * mp + 2) / 5 + 1;
    let month = if mp < 10 { mp + 3 } else { mp - 9 };
    (yoe + era * 400 + i64::from(month <= 2), month, day)
}

/// `date.weekday()`: Monday is 0.
pub(crate) fn weekday(days: i64) -> i64 {
    (days + 3).rem_euclid(7)
}

/// (year, month, day) of a UTC instant.
pub(crate) fn date_of(micros: i64) -> (i64, i64, i64) {
    civil_from_days(micros.div_euclid(DAY))
}

pub(crate) fn days_in_month(year: i64, month: i64) -> i64 {
    match month {
        1 | 3 | 5 | 7 | 8 | 10 | 12 => 31,
        4 | 6 | 9 | 11 => 30,
        _ if year % 4 == 0 && (year % 100 != 0 || year % 400 == 0) => 29,
        _ => 28,
    }
}

/// `datetime(year, month, day, tzinfo=utc)`; a year outside 1..=9999 is an error.
pub(crate) fn midnight(year: i64, month: i64, day: i64) -> Result<i64, PyError> {
    if !(1..=9999).contains(&year) || !(1..=12).contains(&month) || day < 1 || day > days_in_month(year, month) {
        return Err(PyError::TimeOutOfRange);
    }
    Ok(days_from_civil(year, month, day) * DAY)
}
