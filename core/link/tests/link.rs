//! The Link, with two (or three) endpoints in one process on 127.0.0.1.
//! Relays and address lookup are off: no test here uses the internet.

mod common;

use std::sync::atomic::{AtomicUsize, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use common::{admit, device, offline};
use muse_link::frame::{self, read_frame, FrameType, MAX_REQUEST_BODY, MAX_RESPONSE_BODY, MAX_SYNC_MESSAGE};
use muse_link::handler::BoxFuture;
use muse_link::registry::{DeviceRegistry, Role};
use muse_link::{LinkAddr, LinkError, LinkHandler, LoopbackHandler, Peer, RefusalCode, Request, Response};

/// Counts calls; `/v1/big/<n>` answers with n bytes, `/v1/slow` takes a while, the rest echoes.
#[derive(Default)]
struct Counting {
    calls: AtomicUsize,
}

impl LinkHandler for Counting {
    fn handle<'a>(&'a self, peer: &'a Peer, request: Request) -> BoxFuture<'a, Response> {
        Box::pin(async move {
            self.calls.fetch_add(1, Ordering::SeqCst);
            if let Some(n) = request.path.strip_prefix("/v1/big/") {
                return Response { status: 200, body: vec![0x61; n.parse().unwrap()] };
            }
            if request.path == "/v1/slow" {
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
            if request.path == "/v1/role" {
                return Response::new(200, peer.role.name().as_bytes());
            }
            Response { status: 201, body: request.body }
        })
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn request_and_response_round_trip_with_the_authenticated_peer() {
    let home = device(1, offline()).await;
    let phone = device(2, offline()).await;
    admit(&home, &phone, Role::Viewer);
    home.endpoint.serve(Arc::new(LoopbackHandler));
    let client = phone.endpoint.client(home.endpoint.addr()).unwrap();

    let who = client.request("GET", "/v1/whoami", b"").await.unwrap();
    assert_eq!(who.status, 200);
    let text = String::from_utf8(who.body).unwrap();
    // The handler is told who called by the Link, not by the request.
    assert!(text.contains(&muse_core::records::to_hex(&phone.device_id())), "{text}");
    assert!(text.contains("\"role\":\"viewer\""), "{text}");

    let echo = client.request("POST", "/v1/jobs?x=1", b"{\"prompt\":\"hi\"}").await.unwrap();
    assert_eq!((echo.status, echo.body.as_slice()), (200, &b"{\"prompt\":\"hi\"}"[..]));
    assert_eq!(home.endpoint.open_connections(), 1);
    assert_eq!(client.next_attempt_in().await, Duration::ZERO);

    // The address is the link id plus a loopback address; no relay.
    let addr = home.endpoint.addr();
    assert_eq!(addr.id, home.endpoint.id());
    assert!(addr.relay.is_none());
    assert!(addr.direct.iter().all(|a| a.ip().is_loopback()));
    client.close().await;
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn bodies_up_to_the_limit_pass_and_larger_ones_do_not() {
    let home = device(3, offline()).await;
    let phone = device(4, offline()).await;
    admit(&home, &phone, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let client = phone.endpoint.client(home.endpoint.addr()).unwrap();

    let big: Vec<u8> = (0..MAX_REQUEST_BODY).map(|i| (i % 251) as u8).collect();
    let echoed = client.request("POST", "/v1/echo", &big).await.unwrap();
    assert_eq!(echoed.status, 201);
    assert!(echoed.body == big);

    // One byte more is refused here, before anything is sent.
    let calls = handler.calls.load(Ordering::SeqCst);
    let too_big = vec![0u8; MAX_REQUEST_BODY + 1];
    assert_eq!(client.request("POST", "/v1/echo", &too_big).await, Err(LinkError::Frame(frame::FrameError::TooLarge)));
    assert_eq!(handler.calls.load(Ordering::SeqCst), calls);

    let largest = client.request("GET", &format!("/v1/big/{MAX_RESPONSE_BODY}"), b"").await.unwrap();
    assert_eq!(largest.body.len(), MAX_RESPONSE_BODY);
    // A handler that answers with more than the limit: the link says so instead of sending it.
    match client.request("GET", &format!("/v1/big/{}", MAX_RESPONSE_BODY + 1), b"").await {
        Err(LinkError::Refused(r)) => assert_eq!(r.code, RefusalCode::Internal),
        other => panic!("{other:?}"),
    }
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn an_unknown_device_is_refused_before_the_handler_sees_anything() {
    let home = device(5, offline()).await;
    let stranger = device(6, offline()).await;
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let client = stranger.endpoint.client(home.endpoint.addr()).unwrap();
    for _ in 0..2 {
        assert_eq!(client.request("GET", "/v1/status", b"").await, Err(LinkError::NotPaired));
    }
    assert_eq!(handler.calls.load(Ordering::SeqCst), 0);
    assert_eq!(home.endpoint.open_connections(), 0);
    // Every refused attempt makes the client wait longer before the next.
    assert!(client.next_attempt_in().await > Duration::ZERO);

    // An endpoint that serves nothing offers no protocol at all: the handshake itself fails.
    let silent = device(7, offline()).await;
    admit(&silent, &stranger, Role::Owner);
    let mut quick = offline();
    quick.connect_timeout = Duration::from_millis(700);
    quick.connect_attempts = 1;
    let impatient = device(8, quick).await;
    let client = impatient.endpoint.client(silent.endpoint.addr()).unwrap();
    assert!(matches!(client.request("GET", "/v1/status", b"").await, Err(LinkError::Unreachable(_))));
    for d in [&home, &stranger, &silent, &impatient] {
        d.endpoint.close().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_removed_device_is_cut_off_mid_session_and_refused_on_reconnect() {
    let home = device(9, offline()).await;
    let phone = device(10, offline()).await;
    let tablet = device(11, offline()).await;
    admit(&home, &phone, Role::Owner);
    admit(&home, &tablet, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let phone_client = phone.endpoint.client(home.endpoint.addr()).unwrap();
    let tablet_client = tablet.endpoint.client(home.endpoint.addr()).unwrap();
    assert_eq!(phone_client.request("GET", "/v1/role", b"").await.unwrap().body, b"owner");
    assert_eq!(tablet_client.request("GET", "/v1/role", b"").await.unwrap().body, b"owner");
    assert_eq!(home.endpoint.open_connections(), 2);

    // Removal through the endpoint closes the open connection at once.
    let removed = home.endpoint.remove_device(&phone.device_id()).unwrap();
    assert_eq!(removed.link_id, phone.endpoint.id());
    assert_eq!(home.endpoint.open_connections(), 1);
    let calls = handler.calls.load(Ordering::SeqCst);
    for _ in 0..3 {
        // The kept connection is gone; the reconnect is refused too.
        assert_eq!(phone_client.request("GET", "/v1/role", b"").await, Err(LinkError::NotPaired));
    }
    assert_eq!(handler.calls.load(Ordering::SeqCst), calls);
    // The same keys cannot be added back.
    assert!(home.registry.add(phone.entry(Role::Owner)).is_err());

    // Removal behind the endpoint's back (registry only): the next stream on
    // the still-open connection is refused and the connection closed.
    home.registry.remove(&tablet.device_id()).unwrap();
    match tablet_client.request("GET", "/v1/role", b"").await {
        Err(LinkError::Refused(r)) => assert_eq!(r.code, RefusalCode::Removed),
        Err(LinkError::NotPaired) => {}
        other => panic!("{other:?}"),
    }
    assert_eq!(tablet_client.request("GET", "/v1/role", b"").await, Err(LinkError::NotPaired));
    assert_eq!(handler.calls.load(Ordering::SeqCst), calls);
    for d in [&home, &phone, &tablet] {
        d.endpoint.close().await;
    }
}

/// Sends `bytes` on a new stream and returns the frame that comes back.
async fn raw_exchange(client: &muse_link::LinkClient, bytes: &[u8], finish: bool) -> Result<frame::Frame, String> {
    let conn = client.raw_connection().await.map_err(|e| e.to_string())?;
    let (mut send, mut recv) = conn.open_bi().await.map_err(|e| e.to_string())?;
    send.write_all(bytes).await.map_err(|e| e.to_string())?;
    if finish {
        send.finish().map_err(|e| e.to_string())?;
    }
    let frame = tokio::time::timeout(Duration::from_secs(10), read_frame(&mut recv)).await.map_err(|_| "no answer".to_string())?;
    drop(send);
    frame.map_err(|e| e.to_string())
}

fn refusal(frame: Result<frame::Frame, String>) -> RefusalCode {
    let frame = frame.expect("a frame");
    assert_eq!(frame.kind, FrameType::Refusal);
    frame::Refusal::decode(&frame.payload).unwrap().code
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn malformed_oversized_and_truncated_frames_are_refused_without_reaching_the_handler() {
    let home = device(12, offline()).await;
    let phone = device(13, offline()).await;
    admit(&home, &phone, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let client = phone.endpoint.client(home.endpoint.addr()).unwrap();

    let good = frame::encode(FrameType::Request, &Request::new("GET", "/v1/x", b"").encode().unwrap()).unwrap();
    // Not our magic; another version; an unknown type.
    for (at, value) in [(0usize, b'X'), (2, 2), (3, 0x7f)] {
        let mut bad = good.clone();
        bad[at] = value;
        assert_eq!(refusal(raw_exchange(&client, &bad, true).await), RefusalCode::BadRequest);
    }
    // A header that names more than the limit: refused on the header alone
    // (the body is never sent here, and the stream is left open).
    let mut huge = good[..8].to_vec();
    huge[4..8].copy_from_slice(&(u32::MAX).to_be_bytes());
    assert_eq!(refusal(raw_exchange(&client, &huge, false).await), RefusalCode::BadRequest);
    // A frame type that cannot start a stream.
    let response = frame::encode(FrameType::Response, &Response::new(200, b"").encode().unwrap()).unwrap();
    assert_eq!(refusal(raw_exchange(&client, &response, true).await), RefusalCode::BadRequest);
    // Cut short at every point, then the stream ends.
    for cut in [1, 7, 8, good.len() - 1] {
        assert_eq!(refusal(raw_exchange(&client, &good[..cut], true).await), RefusalCode::BadRequest, "cut at {cut}");
    }
    // A well-formed frame around a malformed request (lower-case method; path with a space; trailing byte).
    for payload in [&b"\x03get\x00\x02/v\x00\x00\x00\x00"[..], &b"\x03GET\x00\x03/ v\x00\x00\x00\x00"[..], &b"\x03GET\x00\x02/v\x00\x00\x00\x00\x00"[..]] {
        let bytes = frame::encode(FrameType::Request, payload).unwrap();
        assert_eq!(refusal(raw_exchange(&client, &bytes, true).await), RefusalCode::BadRequest);
    }
    // A loop of altered frames: whatever comes back is a refusal or a real answer, and the link stays up.
    let mut state = 0x1234_5678_9abc_def0u64;
    for _ in 0..40 {
        state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
        let mut bytes = good.clone();
        let at = (state >> 33) as usize % bytes.len();
        bytes[at] ^= 1 << ((state >> 20) % 8);
        let frame = raw_exchange(&client, &bytes, true).await.expect("an answer");
        assert!(matches!(frame.kind, FrameType::Refusal | FrameType::Response));
    }
    assert_eq!(handler.calls.load(Ordering::SeqCst) <= 40, true);
    // After all that the same connection still serves a proper request.
    assert_eq!(client.request("POST", "/v1/echo", b"ok").await.unwrap().body, b"ok");
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_slow_sender_is_timed_out_and_does_not_hold_up_others() {
    let mut config = offline();
    config.request_read_timeout = Duration::from_millis(400);
    config.handler_timeout = Duration::from_millis(600);
    let home = device(14, config).await;
    let phone = device(15, offline()).await;
    admit(&home, &phone, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let client = Arc::new(phone.endpoint.client(home.endpoint.addr()).unwrap());

    let good = frame::encode(FrameType::Request, &Request::new("POST", "/v1/x", &[7u8; 1000]).encode().unwrap()).unwrap();
    // Eight streams that each send part of a request and then nothing.
    let mut slow = Vec::new();
    for i in 0..8usize {
        let client = client.clone();
        let part = good[..3 + i * 50].to_vec();
        slow.push(tokio::spawn(async move { raw_exchange(&client, &part, false).await }));
    }
    // Meanwhile a proper request is served promptly.
    let started = Instant::now();
    assert_eq!(client.request("POST", "/v1/echo", b"quick").await.unwrap().body, b"quick");
    assert!(started.elapsed() < Duration::from_millis(350), "{:?}", started.elapsed());
    for task in slow {
        assert_eq!(refusal(task.await.unwrap()), RefusalCode::Timeout);
    }
    assert_eq!(handler.calls.load(Ordering::SeqCst), 1);
    // A handler that takes too long is cut off the same way.
    match client.request("GET", "/v1/slow", b"").await {
        Err(LinkError::Refused(r)) => assert_eq!(r.code, RefusalCode::Timeout),
        other => panic!("{other:?}"),
    }
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 4)]
async fn many_concurrent_requests_all_complete_within_the_stream_limit() {
    let mut config = offline();
    config.requests_per_second = 10_000;
    config.burst = 10_000;
    config.max_streams_per_connection = 8;
    let home = device(16, config).await;
    let phone = device(17, offline()).await;
    admit(&home, &phone, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let client = Arc::new(phone.endpoint.client(home.endpoint.addr()).unwrap());
    let mut tasks = Vec::new();
    for i in 0..300u32 {
        let client = client.clone();
        tasks.push(tokio::spawn(async move {
            let body = format!("request-{i}-{}", "x".repeat((i % 50) as usize * 100));
            let answer = client.request("POST", "/v1/echo", body.as_bytes()).await.unwrap();
            assert_eq!(answer.body, body.as_bytes());
        }));
    }
    for task in tasks {
        task.await.unwrap();
    }
    assert_eq!(handler.calls.load(Ordering::SeqCst), 300);
    // All of it went over one connection.
    assert_eq!(home.endpoint.open_connections(), 1);
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn each_device_has_its_own_request_rate() {
    let mut config = offline();
    config.requests_per_second = 5;
    config.burst = 5;
    let home = device(18, config).await;
    let greedy = device(19, offline()).await;
    let polite = device(20, offline()).await;
    admit(&home, &greedy, Role::Owner);
    admit(&home, &polite, Role::Owner);
    let handler = Arc::new(Counting::default());
    home.endpoint.serve(handler.clone());
    let greedy_client = greedy.endpoint.client(home.endpoint.addr()).unwrap();
    let polite_client = polite.endpoint.client(home.endpoint.addr()).unwrap();
    let (mut served, mut limited) = (0, 0);
    for _ in 0..30 {
        match greedy_client.request("POST", "/v1/echo", b"x").await {
            Ok(_) => served += 1,
            Err(LinkError::Refused(r)) if r.code == RefusalCode::RateLimited => limited += 1,
            other => panic!("{other:?}"),
        }
    }
    assert!((5..=12).contains(&served), "served {served}");
    assert_eq!(served + limited, 30);
    assert_eq!(handler.calls.load(Ordering::SeqCst), served);
    // Another device is not affected, and the greedy one is served again after a pause.
    assert!(polite_client.request("POST", "/v1/echo", b"y").await.is_ok());
    tokio::time::sleep(Duration::from_millis(600)).await;
    assert!(greedy_client.request("POST", "/v1/echo", b"z").await.is_ok());
    for d in [&home, &greedy, &polite] {
        d.endpoint.close().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_device_cannot_hold_more_connections_than_its_share() {
    let mut config = offline();
    config.max_connections_per_peer = 2;
    let home = device(21, config).await;
    let phone = device(22, offline()).await;
    admit(&home, &phone, Role::Owner);
    home.endpoint.serve(Arc::new(LoopbackHandler));
    let first = phone.endpoint.client(home.endpoint.addr()).unwrap();
    let second = phone.endpoint.client(home.endpoint.addr()).unwrap();
    let third = phone.endpoint.client(home.endpoint.addr()).unwrap();
    assert!(first.request("POST", "/v1/echo", b"1").await.is_ok());
    assert!(second.request("POST", "/v1/echo", b"2").await.is_ok());
    assert_eq!(third.request("POST", "/v1/echo", b"3").await, Err(LinkError::Busy));
    assert_eq!(home.endpoint.open_connections(), 2);
    // Closing one makes room.
    first.close().await;
    tokio::time::sleep(Duration::from_millis(200)).await;
    assert!(third.request("POST", "/v1/echo", b"3").await.is_ok());
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_client_reconnects_after_a_lost_connection_and_backs_off_when_nobody_answers() {
    let home = device(23, offline()).await;
    let mut quick = offline();
    quick.connect_timeout = Duration::from_millis(300);
    quick.connect_attempts = 3;
    quick.backoff_min = Duration::from_millis(100);
    let phone = device(24, quick).await;
    admit(&home, &phone, Role::Owner);
    home.endpoint.serve(Arc::new(LoopbackHandler));
    let client = phone.endpoint.client(home.endpoint.addr()).unwrap();
    assert!(client.request("POST", "/v1/echo", b"1").await.is_ok());
    // The connection is closed under the client; the next request opens a new one.
    client.close().await;
    assert!(client.request("POST", "/v1/echo", b"2").await.is_ok());
    assert_eq!(client.next_attempt_in().await, Duration::ZERO);

    // Nobody listens at this address: three attempts, each after a longer wait.
    let absent = muse_link::LinkKey::from_bytes([200; 32]).public();
    let lost = phone.endpoint.client(LinkAddr { id: absent, relay: None, direct: vec!["127.0.0.1:9".parse().unwrap()] }).unwrap();
    let started = Instant::now();
    assert!(matches!(lost.request("GET", "/v1/status", b"").await, Err(LinkError::Unreachable(_))));
    // Waits of at least 100 ms and 200 ms between the three attempts (each of which may also take up to 300 ms).
    assert!(started.elapsed() >= Duration::from_millis(300), "{:?}", started.elapsed());
    let owed = lost.next_attempt_in().await;
    assert!(owed > Duration::from_millis(50) && owed <= Duration::from_millis(600), "{owed:?}");
    home.endpoint.close().await;
    phone.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_sync_channel_carries_bounded_messages_in_order_both_ways() {
    let home = device(25, offline()).await;
    let phone = device(26, offline()).await;
    admit(&home, &phone, Role::Owner);
    home.endpoint.serve(Arc::new(LoopbackHandler));
    let client = phone.endpoint.client(home.endpoint.addr()).unwrap();
    let mut channel = client.open_sync("docs/settings").await.unwrap();
    assert_eq!(channel.name(), "docs/settings");
    let largest = vec![0x5a; MAX_SYNC_MESSAGE];
    for message in [&b"first"[..], &b""[..], &largest[..], &b"last"[..]] {
        channel.send(message).await.unwrap();
        assert!(channel.recv().await.unwrap().unwrap() == message);
    }
    assert_eq!(channel.send(&vec![0u8; MAX_SYNC_MESSAGE + 1]).await, Err(LinkError::Frame(frame::FrameError::TooLarge)));
    channel.close().await.unwrap();

    // A second channel while the first is gone; a bad name is refused before anything is sent.
    let mut again = client.open_sync("chores").await.unwrap();
    again.send(b"x").await.unwrap();
    assert_eq!(again.recv().await.unwrap().unwrap(), b"x");
    assert!(client.open_sync("").await.is_err());
    assert!(client.open_sync(&"n".repeat(65)).await.is_err());
    match client.open_sync("bad name").await {
        Err(LinkError::Refused(r)) => assert_eq!(r.code, RefusalCode::BadRequest),
        other => panic!("{:?}", other.map(|_| ())),
    }
    // A stranger cannot open one.
    let stranger = device(27, offline()).await;
    let outside = stranger.endpoint.client(home.endpoint.addr()).unwrap();
    assert!(matches!(outside.open_sync("chores").await.map(|_| ()), Err(LinkError::NotPaired)));
    for d in [&home, &phone, &stranger] {
        d.endpoint.close().await;
    }
}
