//! The Muse device link.
//!
//! - [`endpoint`]: a device's end of the Link (iroh underneath: devices dial
//!   each other by public key, directly or through a relay, with no open
//!   port), the request/response protocol for the home control API, and
//!   opaque sync channels. Only paired devices get in.
//! - [`registry`]: the paired devices, their roles, and whether each is still paired.
//! - [`pairing`]: adding a device with a QR code, a one-time secret and a
//!   four-word check; and "I lost this device".
//! - [`unix_http`]: a handler that forwards calls to the home's Unix socket.
//!
//! The contract is docs/spec/interfaces/link-and-pairing.md. Networking and
//! the async runtime live in this crate only; `muse-core` stays pure.
//!
//! The pairing protocol has not had an outside cryptographic review.

#![forbid(unsafe_code)]

pub mod endpoint;
pub mod error;
pub mod frame;
pub mod handler;
pub mod pairing;
pub mod registry;
#[cfg(unix)]
pub mod unix_http;

pub use endpoint::{system_clock, Clock, LinkAddr, LinkClient, LinkConfig, LinkEndpoint, LinkKey, PairingPolicy, RelayChoice, SyncChannel, LINK_ALPN, PAIR_ALPN};
pub use error::LinkError;
pub use frame::{Refusal, RefusalCode, Request, Response};
pub use handler::{LinkHandler, LoopbackHandler, Peer};
pub use pairing::{Invitation, InviterSession, JoinerSession, Pairing, PairingError, PairingState};
pub use registry::{DeviceEntry, DeviceId, DeviceRegistry, DeviceState, LinkId, MemoryRegistry, Role};
