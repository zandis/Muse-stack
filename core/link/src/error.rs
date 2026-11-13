//! One error type for the Link.

use crate::frame::{FrameError, Refusal};

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum LinkError {
    /// The configuration cannot be used (for example, a relay address that is not a URL).
    Config(String),
    /// The endpoint could not be created.
    Bind(String),
    /// The other device could not be reached (after the configured attempts).
    Unreachable(String),
    /// The other device answered at the transport level and then closed the
    /// connection because this device is not paired with it, or was removed.
    NotPaired,
    /// The other device has too many connections open.
    Busy,
    /// The link refused the request before the handler saw it.
    Refused(Refusal),
    /// The request cannot be sent as given, or the reply was not a valid frame.
    Frame(FrameError),
    /// No reply in time.
    Timeout,
    /// The connection or stream ended.
    Closed(String),
    /// This endpoint was closed.
    EndpointClosed,
}

impl std::fmt::Display for LinkError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            LinkError::Config(e) => write!(f, "link configuration: {e}"),
            LinkError::Bind(e) => write!(f, "could not start the link: {e}"),
            LinkError::Unreachable(e) => write!(f, "the other device could not be reached: {e}"),
            LinkError::NotPaired => f.write_str("this device is not paired with the other one"),
            LinkError::Busy => f.write_str("the other device has too many connections open"),
            LinkError::Refused(r) => write!(f, "refused by the link ({:?}): {}", r.code, r.text),
            LinkError::Frame(e) => write!(f, "{e}"),
            LinkError::Timeout => f.write_str("no reply in time"),
            LinkError::Closed(e) => write!(f, "the connection ended: {e}"),
            LinkError::EndpointClosed => f.write_str("the link is closed"),
        }
    }
}

impl std::error::Error for LinkError {}

impl From<FrameError> for LinkError {
    fn from(e: FrameError) -> Self {
        LinkError::Frame(e)
    }
}
