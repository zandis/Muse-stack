//! The exceptions the Python functions can raise, as one small enum.

use std::fmt;

/// Why a function that returns a value in the normal case could not.
///
/// Each variant stands for the Python exception raised by the same input.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum PyError {
    /// A timestamp `datetime` cannot hold: NaN, infinite, or outside the years
    /// 1..=9999 (also when a computed midnight or month start leaves that range).
    /// CPython raises `ValueError`, `OverflowError` or `OSError` depending on the
    /// value and platform; they are one case here.
    TimeOutOfRange,
    /// `ValueError`: text that is not a number where one is required.
    Value,
    /// `OverflowError`: an integer too large to become a float.
    Overflow,
    /// `ZeroDivisionError`: a limit of 0 where a fraction of it is needed.
    ZeroDivision,
    /// `AssertionError`: a documented precondition does not hold.
    Assertion,
    /// `RecursionError`: JSON nested deeper than [`crate::pyjson::MAX_DEPTH`].
    Recursion,
}

impl fmt::Display for PyError {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(match self {
            PyError::TimeOutOfRange => "timestamp is outside the years 1..=9999",
            PyError::Value => "not a number",
            PyError::Overflow => "integer too large for a float",
            PyError::ZeroDivision => "division by a limit of zero",
            PyError::Assertion => "the limit has no window start or reset",
            PyError::Recursion => "JSON is nested too deeply",
        })
    }
}

impl std::error::Error for PyError {}
