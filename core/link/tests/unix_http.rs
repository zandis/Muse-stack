//! The Unix-socket adapter, end to end: a phone calls over the Link, the
//! home's endpoint forwards to a tiny HTTP/1.1 server on a Unix socket (a
//! stand-in for the home's control API) and the reply comes back.
#![cfg(unix)]

mod common;

use std::path::PathBuf;
use std::sync::{Arc, Mutex};

use common::{admit, device, offline};
use muse_link::registry::Role;
use muse_link::unix_http::{RoleTokens, UnixHttpHandler};
use tokio::io::{AsyncReadExt, AsyncWriteExt};
use tokio::net::UnixListener;

fn socket_path(name: &str) -> PathBuf {
    let path = std::env::temp_dir().join(format!("muse-link-{}-{name}.sock", std::process::id()));
    let _ = std::fs::remove_file(&path);
    path
}

/// Serves until dropped. Remembers each request's head; answers by path.
fn tiny_home(path: &PathBuf, seen: Arc<Mutex<Vec<String>>>) -> tokio::task::JoinHandle<()> {
    let listener = UnixListener::bind(path).unwrap();
    tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else { return };
            let seen = seen.clone();
            tokio::spawn(async move {
                let mut raw = Vec::new();
                let mut buf = [0u8; 4096];
                // Read the head, then the body by Content-Length.
                let (head, body) = loop {
                    let n = stream.read(&mut buf).await.unwrap();
                    if n == 0 {
                        return;
                    }
                    raw.extend_from_slice(&buf[..n]);
                    if let Some(at) = raw.windows(4).position(|w| w == b"\r\n\r\n") {
                        let head = String::from_utf8(raw[..at].to_vec()).unwrap();
                        let len: usize = head
                            .lines()
                            .find_map(|l| l.strip_prefix("Content-Length: "))
                            .map(|v| v.trim().parse().unwrap())
                            .unwrap_or(0);
                        while raw.len() < at + 4 + len {
                            let n = stream.read(&mut buf).await.unwrap();
                            raw.extend_from_slice(&buf[..n]);
                        }
                        break (head, raw[at + 4..at + 4 + len].to_vec());
                    }
                };
                seen.lock().unwrap().push(head.clone());
                let request_line = head.lines().next().unwrap().to_string();
                let auth = head.lines().find_map(|l| l.strip_prefix("Authorization: Bearer ")).unwrap_or("").to_string();
                let (status, reply) = if request_line.starts_with("GET /v1/status ") {
                    (200, format!("{{\"ok\":true,\"token_was\":\"{auth}\"}}").into_bytes())
                } else if request_line.starts_with("POST /v1/echo ") {
                    (201, body)
                } else if request_line.starts_with("GET /v1/huge ") {
                    (200, format!("{{\"x\":\"{}\"}}", "a".repeat(muse_home_link::MAX_REPLY)).into_bytes())
                } else {
                    (404, b"{\"error\":{\"code\":\"not_found\"}}".to_vec())
                };
                let head = format!("HTTP/1.1 {status} X\r\nContent-Type: application/json\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", reply.len());
                let _ = stream.write_all(head.as_bytes()).await;
                let _ = stream.write_all(&reply).await;
                let _ = stream.shutdown().await;
            });
        }
    })
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn calls_are_forwarded_to_the_home_socket_with_the_homes_own_token() {
    let path = socket_path("forward");
    let seen = Arc::new(Mutex::new(Vec::new()));
    let server = tiny_home(&path, seen.clone());

    let home = device(40, offline()).await;
    let owner = device(41, offline()).await;
    let viewer = device(42, offline()).await;
    admit(&home, &owner, Role::Owner);
    admit(&home, &viewer, Role::Viewer);
    // Only the owner role has a token on this home.
    let tokens = RoleTokens { owner: Some("owner-token-123".into()), host: None, viewer: None };
    home.endpoint.serve(Arc::new(UnixHttpHandler::new(&path, Arc::new(tokens))));

    let client = owner.endpoint.client(home.endpoint.addr()).unwrap();
    let status = client.request("GET", "/v1/status", b"").await.unwrap();
    assert_eq!(status.status, 200);
    let body: serde_json::Value = serde_json::from_slice(&status.body).unwrap();
    // The token was added on the home side; the phone never had it.
    assert_eq!(body["token_was"], "owner-token-123");

    let echo = client.request("POST", "/v1/echo", br#"{"prompt":"hi"}"#).await.unwrap();
    assert_eq!(echo.status, 201);
    assert_eq!(serde_json::from_slice::<serde_json::Value>(&echo.body).unwrap()["prompt"], "hi");

    let missing = client.request("GET", "/v1/nothing", b"").await.unwrap();
    assert_eq!(missing.status, 404);

    // Refused before the home sees it: not JSON, not an object, not an API path, a header smuggled in.
    let before = seen.lock().unwrap().len();
    for (method, path, body) in [("POST", "/v1/echo", &b"not json"[..]), ("POST", "/v1/echo", b"[1]"), ("GET", "/etc/passwd", b""), ("GET", "/v1/a%0d%0aX:y/../b", b"")] {
        let r = client.request(method, path, body).await.unwrap();
        assert_eq!(r.status, 400, "{method} {path}");
    }
    assert_eq!(seen.lock().unwrap().len(), before);

    // A reply larger than the home's limit is not passed on.
    let huge = client.request("GET", "/v1/huge", b"").await.unwrap();
    assert_eq!(huge.status, 502);

    // A device whose role has no token on the home gets 403 and the home sees nothing.
    let vclient = viewer.endpoint.client(home.endpoint.addr()).unwrap();
    let denied = vclient.request("GET", "/v1/status", b"").await.unwrap();
    assert_eq!(denied.status, 403);
    assert!(String::from_utf8_lossy(&denied.body).contains("no_token_for_device"));

    // The home's socket goes away: 502, not a hang.
    server.abort();
    let _ = std::fs::remove_file(&path);
    let gone = client.request("GET", "/v1/status", b"").await.unwrap();
    assert_eq!(gone.status, 502);

    for head in seen.lock().unwrap().iter() {
        assert!(head.contains("Authorization: Bearer owner-token-123"));
    }
    for d in [&home, &owner, &viewer] {
        d.endpoint.close().await;
    }
}
