//! What the serving side plugs in.

use std::future::Future;
use std::pin::Pin;

use crate::endpoint::SyncChannel;
use crate::frame::{Request, Response};
use crate::registry::{DeviceEntry, DeviceId, LinkId, Role};

pub type BoxFuture<'a, T> = Pin<Box<dyn Future<Output = T> + Send + 'a>>;

/// The device a request came from, as authenticated by the Link and found in
/// the registry. The handler decides what that device may do from `role`
/// (and, later, from the device's mandate); it never needs to trust anything
/// the request itself says about who is calling.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Peer {
    pub device_id: DeviceId,
    pub link_id: LinkId,
    pub label: String,
    pub role: Role,
}

impl Peer {
    pub(crate) fn from_entry(entry: &DeviceEntry) -> Peer {
        Peer { device_id: entry.device_id, link_id: entry.link_id, label: entry.label.clone(), role: entry.role }
    }

    pub fn device_hex(&self) -> String {
        muse_core::records::to_hex(&self.device_id)
    }
}

/// The home side of the Link.
pub trait LinkHandler: Send + Sync + 'static {
    /// One home API call. Runs under the link's handler timeout.
    fn handle<'a>(&'a self, peer: &'a Peer, request: Request) -> BoxFuture<'a, Response>;

    /// A sync channel the peer opened (the sync package's business). The
    /// default closes it at once.
    fn sync<'a>(&'a self, _peer: &'a Peer, channel: SyncChannel) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            let _ = channel.close().await;
        })
    }
}

/// A handler for tests and the example: answers `GET /v1/whoami` with the
/// caller's device and role, echoes any other body back, and echoes sync messages.
#[derive(Debug, Default, Clone, Copy)]
pub struct LoopbackHandler;

impl LinkHandler for LoopbackHandler {
    fn handle<'a>(&'a self, peer: &'a Peer, request: Request) -> BoxFuture<'a, Response> {
        Box::pin(async move {
            if request.method == "GET" && request.path == "/v1/whoami" {
                let body = format!("{{\"device\":\"{}\",\"role\":\"{}\"}}", peer.device_hex(), peer.role.name());
                return Response::new(200, body.as_bytes());
            }
            Response { status: 200, body: request.body }
        })
    }

    fn sync<'a>(&'a self, _peer: &'a Peer, mut channel: SyncChannel) -> BoxFuture<'a, ()> {
        Box::pin(async move {
            while let Ok(Some(message)) = channel.recv().await {
                if channel.send(&message).await.is_err() {
                    return;
                }
            }
            let _ = channel.close().await;
        })
    }
}
