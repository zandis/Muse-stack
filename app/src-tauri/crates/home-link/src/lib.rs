//! How the Muse app reaches the home control API
//! (docs/spec/interfaces/home-api.v1.yaml).
//!
//! The home listens on a private Unix socket, `MUSE_HOME/home.sock` (mode 0600), on
//! Linux and macOS, and on `127.0.0.1` on Windows. Every request carries
//! `Authorization: Bearer <device token>`. This crate builds that request, sends
//! it, and parses the reply. The token is read here, from a file only the person
//! can read, and is never handed to the web view: the web view sends
//! `{method, path, body}` and gets `{status, body}` back.
//!
//! The protocol is a small, fixed subset of HTTP/1.1 (one request per connection,
//! `Content-Length` bodies, `Connection: close`), written by hand over
//! `UnixStream` so the app carries no HTTP stack it does not need.

use serde::Serialize;
use serde_json::Value;
use std::fmt;
use std::io::{Read, Write};
use std::path::{Path, PathBuf};
use std::time::Duration;

/// The home accepts bodies of at most 512 KiB.
pub const MAX_REQUEST_BODY: usize = 512 * 1024;
/// More than any reply the home sends; a bound so a wrong peer cannot fill memory.
pub const MAX_REPLY: usize = 8 * 1024 * 1024;
pub const MAX_PATH: usize = 512;
pub const DEFAULT_TCP_PORT: u16 = 4181;
pub const SOCKET_NAME: &str = "home.sock";
/// The file that holds this app's device token, inside MUSE_HOME. Mode 0600.
pub const TOKEN_FILE: &str = "app.token";
pub const TIMEOUT: Duration = Duration::from_secs(15);

/// Outside addresses the app may open, and nothing else. The same list as src/lib/links.ts.
pub const ALLOWED_HOSTS: [&str; 3] = ["chatgpt.com", "claude.ai", "gemini.google.com"];

/// What went wrong. `code()` is the short word the web view receives; it never holds detail.
#[derive(Debug, PartialEq, Eq)]
pub enum Error {
    /// The request is not one this app sends (method, path or body out of bounds).
    InvalidRequest(&'static str),
    /// No usable device token on this computer.
    NoToken(&'static str),
    /// The home could not be reached.
    Unreachable,
    /// Something answered, but not in the home's protocol.
    BadReply(&'static str),
}

impl Error {
    pub fn code(&self) -> &'static str {
        match self {
            Error::InvalidRequest(_) => "invalid_request",
            Error::NoToken(_) => "no_token",
            Error::Unreachable => "unreachable",
            Error::BadReply(_) => "bad_reply",
        }
    }
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::InvalidRequest(why) | Error::NoToken(why) | Error::BadReply(why) => write!(f, "{}: {}", self.code(), why),
            Error::Unreachable => f.write_str(self.code()),
        }
    }
}

impl std::error::Error for Error {}

/// What the web view gets back: the HTTP status and the JSON body (null when there was none).
#[derive(Debug, PartialEq, Serialize)]
pub struct Reply {
    pub status: u16,
    pub body: Value,
}

/// Where the home listens.
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Endpoint {
    Unix(PathBuf),
    /// 127.0.0.1 only.
    Tcp(u16),
}

impl Endpoint {
    /// The Host header the home expects: it checks it on TCP and ignores it on the socket.
    pub fn host(&self) -> String {
        match self {
            Endpoint::Unix(_) => "localhost".to_string(),
            Endpoint::Tcp(port) => format!("127.0.0.1:{port}"),
        }
    }
}

// ---- building the request

const METHODS: [&str; 4] = ["GET", "POST", "PATCH", "DELETE"];

fn path_ok(path: &str) -> Result<(), Error> {
    if !path.starts_with("/v1/") {
        return Err(Error::InvalidRequest("path must start with /v1/"));
    }
    if path.len() > MAX_PATH {
        return Err(Error::InvalidRequest("path too long"));
    }
    // Letters, digits and the few marks the API's paths and queries use. No spaces, no
    // control characters (so no header can be smuggled in), no backslash, no fragment.
    if !path.bytes().all(|b| b.is_ascii_alphanumeric() || b"/_-.,?=&%".contains(&b)) {
        return Err(Error::InvalidRequest("path has a character the API does not use"));
    }
    if path.contains("..") || path.contains("//") {
        return Err(Error::InvalidRequest("path must be plain"));
    }
    Ok(())
}

fn token_ok(token: &str) -> bool {
    !token.is_empty() && token.len() <= 512 && token.bytes().all(|b| b.is_ascii_graphic())
}

/// The bytes of one request. Fails rather than send anything the home's API does not define.
pub fn build_request(method: &str, path: &str, body: Option<&Value>, token: &str, host: &str) -> Result<Vec<u8>, Error> {
    if !METHODS.contains(&method) {
        return Err(Error::InvalidRequest("method"));
    }
    path_ok(path)?;
    if !token_ok(token) {
        return Err(Error::NoToken("the token has a character a token cannot have"));
    }
    if host.is_empty() || !host.bytes().all(|b| b.is_ascii_alphanumeric() || b".:-".contains(&b)) {
        return Err(Error::InvalidRequest("host"));
    }
    let payload = match body {
        None | Some(Value::Null) => None,
        Some(v @ Value::Object(_)) => Some(serde_json::to_vec(v).map_err(|_| Error::InvalidRequest("body"))?),
        Some(_) => return Err(Error::InvalidRequest("a body must be a JSON object")),
    };
    if method == "GET" && payload.is_some() {
        return Err(Error::InvalidRequest("GET takes no body"));
    }
    let len = payload.as_ref().map_or(0, Vec::len);
    if len > MAX_REQUEST_BODY {
        return Err(Error::InvalidRequest("body too large"));
    }
    let mut out = Vec::with_capacity(256 + len);
    out.extend_from_slice(format!("{method} {path} HTTP/1.1\r\n").as_bytes());
    out.extend_from_slice(format!("Host: {host}\r\n").as_bytes());
    out.extend_from_slice(format!("Authorization: Bearer {token}\r\n").as_bytes());
    out.extend_from_slice(b"Accept: application/json\r\n");
    if payload.is_some() {
        out.extend_from_slice(b"Content-Type: application/json\r\n");
    }
    // Always sent: the home answers 411 to a body without it, and 0 is right for none.
    out.extend_from_slice(format!("Content-Length: {len}\r\n").as_bytes());
    out.extend_from_slice(b"Connection: close\r\n\r\n");
    if let Some(p) = payload {
        out.extend_from_slice(&p);
    }
    Ok(out)
}

// ---- parsing the reply

fn find(haystack: &[u8], needle: &[u8]) -> Option<usize> {
    haystack.windows(needle.len()).position(|w| w == needle)
}

/// Parse one complete reply (everything read until the home closed the connection).
pub fn parse_response(raw: &[u8]) -> Result<Reply, Error> {
    let head_end = find(raw, b"\r\n\r\n").ok_or(Error::BadReply("no end of headers"))?;
    let head = std::str::from_utf8(&raw[..head_end]).map_err(|_| Error::BadReply("headers are not text"))?;
    let mut lines = head.split("\r\n");
    let status_line = lines.next().unwrap_or("");
    let mut parts = status_line.splitn(3, ' ');
    let version = parts.next().unwrap_or("");
    if version != "HTTP/1.1" && version != "HTTP/1.0" {
        return Err(Error::BadReply("not HTTP"));
    }
    let code = parts.next().unwrap_or("");
    if code.len() != 3 || !code.bytes().all(|b| b.is_ascii_digit()) {
        return Err(Error::BadReply("status"));
    }
    let status: u16 = code.parse().map_err(|_| Error::BadReply("status"))?;
    if !(200..=599).contains(&status) {
        return Err(Error::BadReply("status"));
    }
    let mut content_length: Option<usize> = None;
    for line in lines {
        let (name, value) = line.split_once(':').ok_or(Error::BadReply("header"))?;
        let name = name.trim().to_ascii_lowercase();
        let value = value.trim();
        if name == "transfer-encoding" {
            return Err(Error::BadReply("the home never sends chunked replies"));
        }
        if name == "content-length" {
            if value.is_empty() || value.len() > 9 || !value.bytes().all(|b| b.is_ascii_digit()) {
                return Err(Error::BadReply("content-length"));
            }
            let n: usize = value.parse().map_err(|_| Error::BadReply("content-length"))?;
            if content_length.is_some_and(|seen| seen != n) {
                return Err(Error::BadReply("two different content-lengths"));
            }
            content_length = Some(n);
        }
    }
    let rest = &raw[head_end + 4..];
    let body = match content_length {
        Some(n) if n > rest.len() => return Err(Error::BadReply("the reply was cut short")),
        Some(n) => &rest[..n],
        None => rest, // Connection: close marks the end
    };
    // Every reply of the home is JSON. Anything else (a proxy's error page) becomes null,
    // and the web view treats a failure status without the API's error shape as "unreachable".
    let body = if body.is_empty() { Value::Null } else { serde_json::from_slice(body).unwrap_or(Value::Null) };
    Ok(Reply { status, body })
}

// ---- where the home is, and the token

/// MUSE_HOME, or `~/.muse`. The same rule as muse/paths.py.
pub fn muse_home(muse_home: Option<&str>, user_home: Option<&str>) -> Option<PathBuf> {
    match muse_home.map(str::trim).filter(|s| !s.is_empty()) {
        Some(dir) => Some(match (dir.strip_prefix("~/"), user_home) {
            (Some(rest), Some(home)) => Path::new(home).join(rest),
            _ => PathBuf::from(dir),
        }),
        None => user_home.filter(|s| !s.is_empty()).map(|h| Path::new(h).join(".muse")),
    }
}

/// The Unix socket where there is one; 127.0.0.1 on Windows, or when `tcp_port` is given
/// (development against `python -m muse.home --fake --tcp PORT`).
pub fn endpoint(home: &Path, tcp_port: Option<&str>) -> Result<Endpoint, Error> {
    if let Some(raw) = tcp_port.map(str::trim).filter(|s| !s.is_empty()) {
        return match raw.parse::<u16>() {
            Ok(port) if port > 0 => Ok(Endpoint::Tcp(port)),
            _ => Err(Error::InvalidRequest("MUSE_HOME_TCP must be a port number")),
        };
    }
    if cfg!(unix) {
        Ok(Endpoint::Unix(home.join(SOCKET_NAME)))
    } else {
        Ok(Endpoint::Tcp(DEFAULT_TCP_PORT))
    }
}

/// Read the device token. On Unix the file must be the person's own and closed to everyone
/// else (no group or other permission bits), or it is not used.
pub fn read_token(path: &Path) -> Result<String, Error> {
    let meta = std::fs::symlink_metadata(path).map_err(|_| Error::NoToken("no token file"))?;
    if !meta.is_file() {
        return Err(Error::NoToken("the token file is not a plain file"));
    }
    if meta.len() > 4096 {
        return Err(Error::NoToken("the token file is too large"));
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::MetadataExt;
        if meta.mode() & 0o077 != 0 {
            return Err(Error::NoToken("the token file can be read by others; set it to mode 0600"));
        }
    }
    let text = std::fs::read_to_string(path).map_err(|_| Error::NoToken("the token file could not be read"))?;
    let token = text.trim();
    if !token_ok(token) {
        return Err(Error::NoToken("the token file does not hold one token"));
    }
    Ok(token.to_string())
}

// ---- sending

fn exchange<S: Read + Write>(mut stream: S, request: &[u8]) -> Result<Reply, Error> {
    stream.write_all(request).map_err(|_| Error::Unreachable)?;
    stream.flush().map_err(|_| Error::Unreachable)?;
    let mut raw = Vec::with_capacity(4096);
    let mut limited = stream.take(MAX_REPLY as u64 + 1);
    limited.read_to_end(&mut raw).map_err(|_| Error::Unreachable)?;
    if raw.len() > MAX_REPLY {
        return Err(Error::BadReply("the reply is too large"));
    }
    if raw.is_empty() {
        return Err(Error::Unreachable);
    }
    parse_response(&raw)
}

/// One call to the home.
pub fn call(endpoint: &Endpoint, token: &str, method: &str, path: &str, body: Option<&Value>, timeout: Duration) -> Result<Reply, Error> {
    let request = build_request(method, path, body, token, &endpoint.host())?;
    match endpoint {
        Endpoint::Tcp(port) => {
            let addr = std::net::SocketAddr::from(([127, 0, 0, 1], *port));
            let stream = std::net::TcpStream::connect_timeout(&addr, timeout).map_err(|_| Error::Unreachable)?;
            stream.set_read_timeout(Some(timeout)).map_err(|_| Error::Unreachable)?;
            stream.set_write_timeout(Some(timeout)).map_err(|_| Error::Unreachable)?;
            exchange(stream, &request)
        }
        #[cfg(unix)]
        Endpoint::Unix(socket) => {
            let stream = std::os::unix::net::UnixStream::connect(socket).map_err(|_| Error::Unreachable)?;
            stream.set_read_timeout(Some(timeout)).map_err(|_| Error::Unreachable)?;
            stream.set_write_timeout(Some(timeout)).map_err(|_| Error::Unreachable)?;
            exchange(stream, &request)
        }
        #[cfg(not(unix))]
        Endpoint::Unix(_) => Err(Error::Unreachable),
    }
}

/// The call the app makes: find the home and the token from the environment, then `call`.
///
/// Environment: `MUSE_HOME` (default `~/.muse`); for development only, `MUSE_HOME_TCP`
/// (a port on 127.0.0.1) and `MUSE_HOME_TOKEN_FILE` (another token file).
pub fn request(method: &str, path: &str, body: Option<&Value>) -> Result<Reply, Error> {
    let var = |name: &str| std::env::var(name).ok();
    let user_home = var("HOME").or_else(|| var("USERPROFILE"));
    let home = muse_home(var("MUSE_HOME").as_deref(), user_home.as_deref()).ok_or(Error::Unreachable)?;
    // Check the request before touching the token, so a bad request never reads it.
    build_request(method, path, body, "x", "localhost")?;
    let ep = endpoint(&home, var("MUSE_HOME_TCP").as_deref())?;
    let token_path = var("MUSE_HOME_TOKEN_FILE").filter(|s| !s.trim().is_empty()).map(PathBuf::from).unwrap_or_else(|| home.join(TOKEN_FILE));
    let token = read_token(&token_path)?;
    call(&ep, &token, method, path, body, TIMEOUT)
}

// ---- outside links

/// The address to open, if it is https and its host is on the list. Checked here again
/// because the web view's own check (src/lib/links.ts) is not something Rust should trust.
pub fn allowed_external(url: &str) -> Option<&str> {
    if url.len() > 2048 || !url.bytes().all(|b| b.is_ascii_graphic()) {
        return None;
    }
    let rest = url.strip_prefix("https://")?;
    let end = rest.find(['/', '?', '#']).unwrap_or(rest.len());
    let host = &rest[..end];
    // No user:password@, no port: the host must be exactly one of the listed names.
    if host.contains('@') || host.contains(':') || host.contains('\\') {
        return None;
    }
    ALLOWED_HOSTS.contains(&host).then_some(url)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn text(bytes: &[u8]) -> String {
        String::from_utf8(bytes.to_vec()).unwrap()
    }

    #[test]
    fn builds_a_get() {
        let req = text(&build_request("GET", "/v1/cards?status=pending", None, "tok", "localhost").unwrap());
        assert_eq!(
            req,
            "GET /v1/cards?status=pending HTTP/1.1\r\nHost: localhost\r\nAuthorization: Bearer tok\r\nAccept: application/json\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        );
    }

    #[test]
    fn builds_a_post_with_a_body_and_its_length() {
        let body = json!({"decision": "approve", "always": true});
        let req = text(&build_request("POST", "/v1/cards/0123456789abcdef/answer", Some(&body), "tok", "127.0.0.1:4181").unwrap());
        let (head, payload) = req.split_once("\r\n\r\n").unwrap();
        assert!(head.starts_with("POST /v1/cards/0123456789abcdef/answer HTTP/1.1\r\nHost: 127.0.0.1:4181\r\n"));
        assert!(head.contains("Content-Type: application/json\r\n"));
        assert!(head.contains(&format!("Content-Length: {}\r\n", payload.len())));
        assert_eq!(serde_json::from_str::<Value>(payload).unwrap(), body);
    }

    #[test]
    fn a_post_without_a_body_still_sends_a_length() {
        let req = text(&build_request("POST", "/v1/pause", Some(&Value::Null), "tok", "localhost").unwrap());
        assert!(req.contains("Content-Length: 0\r\n"));
        assert!(!req.contains("Content-Type"));
        assert!(req.ends_with("\r\n\r\n"));
    }

    #[test]
    fn refuses_anything_the_api_does_not_define() {
        let bad = |m: &str, p: &str| matches!(build_request(m, p, None, "tok", "localhost"), Err(Error::InvalidRequest(_)));
        assert!(bad("PUT", "/v1/status"));
        assert!(bad("get", "/v1/status"));
        assert!(bad("GET", "/v2/status"));
        assert!(bad("GET", "v1/status"));
        assert!(bad("GET", "/v1/../etc/passwd"));
        assert!(bad("GET", "/v1//status"));
        assert!(bad("GET", "/v1/status HTTP/1.1\r\nX-Evil: 1"));
        assert!(bad("GET", "/v1/status\r\nHost: evil"));
        assert!(bad("GET", "/v1/status#frag"));
        assert!(bad("GET", "/v1/a b"));
        assert!(bad("GET", "/v1/caf\u{e9}"));
        assert!(bad("GET", &format!("/v1/{}", "a".repeat(600))));
        assert!(matches!(build_request("GET", "/v1/status", Some(&json!({"a": 1})), "tok", "localhost"), Err(Error::InvalidRequest(_))));
        assert!(matches!(build_request("POST", "/v1/jobs", Some(&json!([1, 2])), "tok", "localhost"), Err(Error::InvalidRequest(_))));
        assert!(matches!(build_request("POST", "/v1/jobs", Some(&json!("text")), "tok", "localhost"), Err(Error::InvalidRequest(_))));
        assert!(matches!(build_request("GET", "/v1/status", None, "tok", "evil host"), Err(Error::InvalidRequest(_))));
        let big = json!({"prompt": "x".repeat(MAX_REQUEST_BODY)});
        assert_eq!(build_request("POST", "/v1/jobs", Some(&big), "tok", "localhost"), Err(Error::InvalidRequest("body too large")));
    }

    #[test]
    fn a_token_can_never_break_out_of_its_header() {
        for token in ["", "a b", "tok\r\nX-Evil: 1", "tok\n", "t\u{f6}k", &"a".repeat(513)] {
            assert!(matches!(build_request("GET", "/v1/status", None, token, "localhost"), Err(Error::NoToken(_))), "{token:?}");
        }
    }

    #[test]
    fn parses_a_reply_from_the_home() {
        let raw = b"HTTP/1.1 200 OK\r\nServer: MuseHome\r\nContent-Type: application/json; charset=utf-8\r\nContent-Length: 15\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n{\"paused\":true}";
        assert_eq!(parse_response(raw).unwrap(), Reply { status: 200, body: json!({"paused": true}) });
    }

    #[test]
    fn parses_an_error_reply_with_its_code() {
        let body = r#"{"error":{"code":"card_already_answered","message":"That card was already answered."}}"#;
        let raw = format!("HTTP/1.1 409 Conflict\r\nContent-Length: {}\r\n\r\n{}", body.len(), body);
        let reply = parse_response(raw.as_bytes()).unwrap();
        assert_eq!(reply.status, 409);
        assert_eq!(reply.body["error"]["code"], "card_already_answered");
    }

    #[test]
    fn keeps_text_in_other_languages_whole() {
        let body = "{\"name\":\"每週摘要\"}";
        let raw = format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\n\r\n{}", body.len(), body);
        assert_eq!(parse_response(raw.as_bytes()).unwrap().body["name"], "每週摘要");
    }

    #[test]
    fn reads_to_the_end_when_no_length_is_given_and_ignores_extra_bytes_when_one_is() {
        assert_eq!(parse_response(b"HTTP/1.0 200 OK\r\n\r\n{\"a\":1}").unwrap().body, json!({"a": 1}));
        assert_eq!(parse_response(b"HTTP/1.1 200 OK\r\nContent-Length: 7\r\n\r\n{\"a\":1}garbage").unwrap().body, json!({"a": 1}));
    }

    #[test]
    fn a_reply_that_is_not_json_has_a_null_body() {
        assert_eq!(parse_response(b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 4\r\n\r\noops").unwrap(), Reply { status: 502, body: Value::Null });
        assert_eq!(parse_response(b"HTTP/1.1 204 No Content\r\nContent-Length: 0\r\n\r\n").unwrap(), Reply { status: 204, body: Value::Null });
    }

    #[test]
    fn refuses_replies_outside_the_protocol() {
        let bad = |raw: &[u8]| matches!(parse_response(raw), Err(Error::BadReply(_)));
        assert!(bad(b""));
        assert!(bad(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n"));
        assert!(bad(b"SSH-2.0-OpenSSH\r\n\r\n"));
        assert!(bad(b"HTTP/1.1 abc OK\r\n\r\n"));
        assert!(bad(b"HTTP/1.1 99 Odd\r\n\r\n"));
        assert!(bad(b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"));
        assert!(bad(b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n{}"));
        assert!(bad(b"HTTP/1.1 200 OK\r\nContent-Length: -1\r\n\r\n{}"));
        assert!(bad(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\nContent-Length: 3\r\n\r\n{}x"));
        assert!(bad(b"HTTP/1.1 200 OK\r\nno colon here\r\n\r\n{}"));
    }

    #[test]
    fn finds_the_home_like_the_python_side() {
        assert_eq!(muse_home(None, Some("/home/mei")), Some(PathBuf::from("/home/mei/.muse")));
        assert_eq!(muse_home(Some("  "), Some("/home/mei")), Some(PathBuf::from("/home/mei/.muse")));
        assert_eq!(muse_home(Some("/srv/muse"), Some("/home/mei")), Some(PathBuf::from("/srv/muse")));
        assert_eq!(muse_home(Some("~/m"), Some("/home/mei")), Some(PathBuf::from("/home/mei/m")));
        assert_eq!(muse_home(None, None), None);
    }

    #[test]
    fn picks_the_socket_or_the_local_port() {
        let home = Path::new("/home/mei/.muse");
        assert_eq!(endpoint(home, Some("4999")), Ok(Endpoint::Tcp(4999)));
        assert!(endpoint(home, Some("evil.example:80")).is_err());
        assert!(endpoint(home, Some("0")).is_err());
        #[cfg(unix)]
        assert_eq!(endpoint(home, None), Ok(Endpoint::Unix(PathBuf::from("/home/mei/.muse/home.sock"))));
        assert_eq!(Endpoint::Tcp(4181).host(), "127.0.0.1:4181");
        assert_eq!(Endpoint::Unix(PathBuf::from("/x")).host(), "localhost");
    }

    fn temp_dir(name: &str) -> PathBuf {
        let dir = std::env::temp_dir().join(format!("muse-home-link-{}-{}", name, std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    #[cfg(unix)]
    #[test]
    fn reads_the_token_only_from_a_private_file() {
        use std::os::unix::fs::PermissionsExt;
        let dir = temp_dir("token");
        let path = dir.join(TOKEN_FILE);
        assert!(matches!(read_token(&path), Err(Error::NoToken(_))));
        std::fs::write(&path, "muse-device-token-1\n").unwrap();
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
        assert!(matches!(read_token(&path), Err(Error::NoToken(_))), "a file others can read is not used");
        std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o600)).unwrap();
        assert_eq!(read_token(&path).unwrap(), "muse-device-token-1");
        std::fs::write(&path, "two tokens\n").unwrap();
        assert!(matches!(read_token(&path), Err(Error::NoToken(_))));
        std::fs::write(&path, "").unwrap();
        assert!(matches!(read_token(&path), Err(Error::NoToken(_))));
        let link = dir.join("link");
        std::os::unix::fs::symlink(&path, &link).unwrap();
        assert!(matches!(read_token(&link), Err(Error::NoToken(_))), "a link is not followed");
        assert!(matches!(read_token(&dir), Err(Error::NoToken(_))));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    /// A pretend home on a real Unix socket: checks what arrives and answers once.
    #[cfg(unix)]
    #[test]
    fn talks_to_a_home_over_its_unix_socket() {
        use std::os::unix::net::UnixListener;
        let dir = temp_dir("sock");
        let socket = dir.join(SOCKET_NAME);
        let listener = UnixListener::bind(&socket).unwrap();
        let server = std::thread::spawn(move || {
            let (mut conn, _) = listener.accept().unwrap();
            let mut seen = Vec::new();
            let mut buf = [0u8; 1024];
            loop {
                let n = conn.read(&mut buf).unwrap();
                seen.extend_from_slice(&buf[..n]);
                if let Some(end) = find(&seen, b"\r\n\r\n") {
                    let head = String::from_utf8_lossy(&seen[..end]).to_string();
                    let len: usize = head.lines().find_map(|l| l.strip_prefix("Content-Length: ")).unwrap().parse().unwrap();
                    if seen.len() >= end + 4 + len {
                        break;
                    }
                }
            }
            let body = br#"{"card":{"status":"approved"}}"#;
            conn.write_all(format!("HTTP/1.1 200 OK\r\nContent-Length: {}\r\nConnection: close\r\n\r\n", body.len()).as_bytes()).unwrap();
            conn.write_all(body).unwrap();
            String::from_utf8(seen).unwrap()
        });
        let reply = call(&Endpoint::Unix(socket), "tok-123", "POST", "/v1/cards/0123456789abcdef/answer", Some(&json!({"decision": "approve"})), Duration::from_secs(5)).unwrap();
        assert_eq!(reply, Reply { status: 200, body: json!({"card": {"status": "approved"}}) });
        let seen = server.join().unwrap();
        assert!(seen.starts_with("POST /v1/cards/0123456789abcdef/answer HTTP/1.1\r\n"));
        assert!(seen.contains("Authorization: Bearer tok-123\r\n"));
        assert!(seen.ends_with(r#"{"decision":"approve"}"#));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn says_unreachable_when_nothing_listens() {
        let dir = temp_dir("none");
        assert_eq!(call(&Endpoint::Unix(dir.join(SOCKET_NAME)), "tok", "GET", "/v1/status", None, Duration::from_secs(1)), Err(Error::Unreachable));
        // A port nothing listens on (bound, then released).
        let port = std::net::TcpListener::bind("127.0.0.1:0").unwrap().local_addr().unwrap().port();
        assert_eq!(call(&Endpoint::Tcp(port), "tok", "GET", "/v1/status", None, Duration::from_secs(1)), Err(Error::Unreachable));
        std::fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn error_codes_hold_no_detail() {
        assert_eq!(Error::InvalidRequest("path too long").code(), "invalid_request");
        assert_eq!(Error::NoToken("x").code(), "no_token");
        assert_eq!(Error::Unreachable.code(), "unreachable");
        assert_eq!(Error::BadReply("x").code(), "bad_reply");
    }

    #[test]
    fn opens_only_https_links_to_listed_hosts() {
        assert_eq!(allowed_external("https://chatgpt.com/"), Some("https://chatgpt.com/"));
        assert_eq!(allowed_external("https://claude.ai/new?q=hello"), Some("https://claude.ai/new?q=hello"));
        assert_eq!(allowed_external("https://gemini.google.com"), Some("https://gemini.google.com"));
        for bad in [
            "http://chatgpt.com/",
            "https://chatgpt.com.evil.example/",
            "https://evil.example/?u=https://chatgpt.com/",
            "https://user:pw@chatgpt.com/",
            "https://chatgpt.com@evil.example/",
            "https://chatgpt.com:8443/",
            "https://CHATGPT.com/",
            "https://chatgpt.com\\@evil.example/",
            "javascript:alert(1)",
            "file:///etc/passwd",
            "tauri://localhost/",
            "https://chatgpt.com/ x",
            "https://chatgpt.com/\n",
            "",
        ] {
            assert_eq!(allowed_external(bad), None, "{bad:?}");
        }
    }
}
