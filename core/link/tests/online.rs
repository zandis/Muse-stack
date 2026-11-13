//! The one test that uses the internet: two endpoints that know each other
//! only by link id and relay URL (no direct address given), through iroh's
//! default public relays. Skipped unless `MUSE_LINK_ONLINE=1`.

mod common;

use std::sync::Arc;
use std::time::Duration;

use common::{admit, device};
use muse_link::registry::Role;
use muse_link::{LinkAddr, LinkConfig, LoopbackHandler, RelayChoice};

fn online() -> LinkConfig {
    LinkConfig {
        relay: RelayChoice::Default,
        // Address lookup stays off: the relay URL is passed explicitly, as an invitation would.
        discovery: false,
        bind: None,
        proxy_from_env: true,
        connect_timeout: Duration::from_secs(30),
        ..LinkConfig::default()
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn round_trip_through_the_public_relays() {
    if std::env::var("MUSE_LINK_ONLINE").as_deref() != Ok("1") {
        eprintln!("skipped: set MUSE_LINK_ONLINE=1 to run the test that uses the public relays");
        return;
    }
    let home = device(60, online()).await;
    let phone = device(61, online()).await;
    admit(&home, &phone, Role::Owner);
    home.endpoint.serve(Arc::new(LoopbackHandler));
    tokio::time::timeout(Duration::from_secs(30), home.endpoint.online()).await.expect("the home reached a relay");
    let relay = home.endpoint.addr().relay.expect("a home relay");
    eprintln!("home relay: {relay}");
    let client = phone.endpoint.client(LinkAddr { id: home.endpoint.id(), relay: Some(relay), direct: Vec::new() }).unwrap();
    let answer = client.request("POST", "/v1/echo", b"over the relay").await.unwrap();
    assert_eq!(answer.body, b"over the relay");
    home.endpoint.close().await;
    phone.endpoint.close().await;
}
