//! muse-core: the portable, pure part of Muse.
//!
//! Each function here returns what the Python function of the same name returns
//! for the same input; `conformance/*.json` (exported from Python by
//! `scripts/export_conformance.py`) is the proof, and `cargo test` checks every
//! vector. No I/O, no clock, no network: `now` is always an argument.
//!
//! - [`quota`]: rate-limit headers and failures -> how long to leave a provider alone.
//! - [`ration`]: quota windows, the pacing line, the options trailer.
//! - [`cache`]: what may be cached and the cache key.
//! - [`pyjson`], [`pytext`]: Python's JSON and text rules where they are observable.

#![forbid(unsafe_code)]

pub mod cache;
mod civil;
pub mod error;
pub mod pyjson;
pub mod pytext;
pub mod quota;
pub mod ration;
pub mod records;

pub use error::PyError;
pub use pyjson::PyJson;
