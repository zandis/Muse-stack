//! Frames: everything either protocol puts on a stream.
//!
//! ```text
//! frame = "ML" | version (1 byte, = 1) | type (1 byte) | length (u32, big-endian) | payload
//! ```
//!
//! The length is checked against the limit for its type **before** anything is
//! allocated or read, so a peer cannot make this side reserve memory by naming
//! a large number. Parsing is strict: an unknown type, another version, a
//! payload with bytes left over or a length that does not fit is an error, and
//! the stream is dropped.

use tokio::io::{AsyncRead, AsyncReadExt, AsyncWrite, AsyncWriteExt};

pub const MAGIC: [u8; 2] = *b"ML";
pub const VERSION: u8 = 1;
pub const HEADER_LEN: usize = 8;

/// A request body (the home accepts 512 KiB; this leaves room and no more).
pub const MAX_REQUEST_BODY: usize = 1024 * 1024;
/// A response body (the home's largest reply is 8 MiB).
pub const MAX_RESPONSE_BODY: usize = 8 * 1024 * 1024;
pub const MAX_METHOD: usize = 16;
pub const MAX_PATH: usize = 2048;
/// One message on a sync channel.
pub const MAX_SYNC_MESSAGE: usize = 1024 * 1024;
pub const MAX_SYNC_NAME: usize = 64;
pub const MAX_REFUSAL_TEXT: usize = 256;
/// One Noise message (the Noise limit).
pub const MAX_NOISE_MESSAGE: usize = 65535;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u8)]
pub enum FrameType {
    Request = 0x01,
    Response = 0x02,
    /// The link itself refused (not the handler): see [`Refusal`].
    Refusal = 0x03,
    SyncOpen = 0x04,
    SyncAccept = 0x05,
    SyncMessage = 0x06,
    SyncClose = 0x07,
    PairHello = 0x20,
    PairNoise = 0x21,
    PairData = 0x22,
    PairAbort = 0x23,
}

impl FrameType {
    pub fn from_u8(byte: u8) -> Option<FrameType> {
        use FrameType::*;
        [Request, Response, Refusal, SyncOpen, SyncAccept, SyncMessage, SyncClose, PairHello, PairNoise, PairData, PairAbort]
            .into_iter()
            .find(|t| *t as u8 == byte)
    }

    /// The largest payload this side reads for the type.
    pub fn max_len(self) -> usize {
        match self {
            FrameType::Request => 1 + MAX_METHOD + 2 + MAX_PATH + 4 + MAX_REQUEST_BODY,
            FrameType::Response => 2 + 4 + MAX_RESPONSE_BODY,
            FrameType::Refusal => 2 + 2 + MAX_REFUSAL_TEXT,
            FrameType::SyncOpen => 1 + MAX_SYNC_NAME,
            FrameType::SyncAccept | FrameType::SyncClose => 0,
            FrameType::SyncMessage => MAX_SYNC_MESSAGE,
            FrameType::PairHello => 16,
            FrameType::PairNoise | FrameType::PairData => MAX_NOISE_MESSAGE,
            FrameType::PairAbort => 1,
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum FrameError {
    /// Not a frame of this protocol: wrong magic, version or type.
    Malformed(&'static str),
    /// The stated length is over the limit for the type.
    TooLarge,
    /// The stream ended inside a frame (or before one).
    Truncated,
    /// The underlying stream failed.
    Io(String),
}

impl std::fmt::Display for FrameError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FrameError::Malformed(what) => write!(f, "malformed frame: {what}"),
            FrameError::TooLarge => f.write_str("frame over the size limit"),
            FrameError::Truncated => f.write_str("stream ended inside a frame"),
            FrameError::Io(e) => write!(f, "stream error: {e}"),
        }
    }
}

impl std::error::Error for FrameError {}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Frame {
    pub kind: FrameType,
    pub payload: Vec<u8>,
}

/// The eight header bytes for a payload of `len` bytes.
pub fn header(kind: FrameType, len: usize) -> Result<[u8; HEADER_LEN], FrameError> {
    if len > kind.max_len() {
        return Err(FrameError::TooLarge);
    }
    let n = (len as u32).to_be_bytes();
    Ok([MAGIC[0], MAGIC[1], VERSION, kind as u8, n[0], n[1], n[2], n[3]])
}

/// Type and payload length from a header; refuses anything over the type's limit.
pub fn parse_header(bytes: &[u8; HEADER_LEN]) -> Result<(FrameType, usize), FrameError> {
    if bytes[0..2] != MAGIC {
        return Err(FrameError::Malformed("magic"));
    }
    if bytes[2] != VERSION {
        return Err(FrameError::Malformed("version"));
    }
    let kind = FrameType::from_u8(bytes[3]).ok_or(FrameError::Malformed("type"))?;
    let len = u32::from_be_bytes([bytes[4], bytes[5], bytes[6], bytes[7]]) as usize;
    if len > kind.max_len() {
        return Err(FrameError::TooLarge);
    }
    Ok((kind, len))
}

pub fn encode(kind: FrameType, payload: &[u8]) -> Result<Vec<u8>, FrameError> {
    let mut out = Vec::with_capacity(HEADER_LEN + payload.len());
    out.extend_from_slice(&header(kind, payload.len())?);
    out.extend_from_slice(payload);
    Ok(out)
}

/// One whole frame from a byte slice, with nothing left over.
pub fn decode(bytes: &[u8]) -> Result<Frame, FrameError> {
    let head: &[u8; HEADER_LEN] = bytes.get(..HEADER_LEN).and_then(|h| h.try_into().ok()).ok_or(FrameError::Truncated)?;
    let (kind, len) = parse_header(head)?;
    match bytes.len() - HEADER_LEN {
        n if n < len => Err(FrameError::Truncated),
        n if n > len => Err(FrameError::Malformed("bytes after the frame")),
        _ => Ok(Frame { kind, payload: bytes[HEADER_LEN..].to_vec() }),
    }
}

fn io_error(e: std::io::Error) -> FrameError {
    if e.kind() == std::io::ErrorKind::UnexpectedEof {
        FrameError::Truncated
    } else {
        FrameError::Io(e.to_string())
    }
}

/// Reads one frame. Memory use is bounded by the type's limit, and grows only
/// as bytes actually arrive (a peer that names 8 MiB and sends nothing costs
/// nothing but the caller's timeout).
pub async fn read_frame<R: AsyncRead + Unpin>(reader: &mut R) -> Result<Frame, FrameError> {
    let mut head = [0u8; HEADER_LEN];
    reader.read_exact(&mut head).await.map_err(io_error)?;
    let (kind, len) = parse_header(&head)?;
    let mut payload = Vec::new();
    let mut taken = reader.take(len as u64);
    taken.read_to_end(&mut payload).await.map_err(io_error)?;
    if payload.len() != len {
        return Err(FrameError::Truncated);
    }
    Ok(Frame { kind, payload })
}

/// Like [`read_frame`], but a clean end of stream before the first byte is `Ok(None)`.
pub async fn read_frame_or_end<R: AsyncRead + Unpin>(reader: &mut R) -> Result<Option<Frame>, FrameError> {
    let mut first = [0u8; 1];
    if reader.read(&mut first).await.map_err(io_error)? == 0 {
        return Ok(None);
    }
    let mut head = [0u8; HEADER_LEN];
    head[0] = first[0];
    reader.read_exact(&mut head[1..]).await.map_err(io_error)?;
    let (kind, len) = parse_header(&head)?;
    let mut payload = Vec::new();
    let mut taken = reader.take(len as u64);
    taken.read_to_end(&mut payload).await.map_err(io_error)?;
    if payload.len() != len {
        return Err(FrameError::Truncated);
    }
    Ok(Some(Frame { kind, payload }))
}

pub async fn write_frame<W: AsyncWrite + Unpin>(writer: &mut W, kind: FrameType, payload: &[u8]) -> Result<(), FrameError> {
    let head = header(kind, payload.len())?;
    writer.write_all(&head).await.map_err(io_error)?;
    writer.write_all(payload).await.map_err(io_error)?;
    Ok(())
}

// ---------------------------------------------------------------- payloads

/// Strict reader over a payload: every `take` is bounds-checked and `end` insists nothing is left.
pub struct Cursor<'a> {
    bytes: &'a [u8],
}

impl<'a> Cursor<'a> {
    pub fn new(bytes: &'a [u8]) -> Self {
        Cursor { bytes }
    }

    pub fn take(&mut self, n: usize) -> Result<&'a [u8], FrameError> {
        if self.bytes.len() < n {
            return Err(FrameError::Malformed("payload too short"));
        }
        let (head, rest) = self.bytes.split_at(n);
        self.bytes = rest;
        Ok(head)
    }

    pub fn array<const N: usize>(&mut self) -> Result<[u8; N], FrameError> {
        let mut out = [0u8; N];
        out.copy_from_slice(self.take(N)?);
        Ok(out)
    }

    pub fn u8(&mut self) -> Result<u8, FrameError> {
        Ok(self.take(1)?[0])
    }

    pub fn u16(&mut self) -> Result<u16, FrameError> {
        Ok(u16::from_be_bytes(self.array()?))
    }

    pub fn u32(&mut self) -> Result<u32, FrameError> {
        Ok(u32::from_be_bytes(self.array()?))
    }

    pub fn u64(&mut self) -> Result<u64, FrameError> {
        Ok(u64::from_be_bytes(self.array()?))
    }

    /// A field with a one-byte length, at most `max` bytes.
    pub fn lp8(&mut self, max: usize) -> Result<&'a [u8], FrameError> {
        let n = self.u8()? as usize;
        if n > max {
            return Err(FrameError::Malformed("field too long"));
        }
        self.take(n)
    }

    pub fn lp16(&mut self, max: usize) -> Result<&'a [u8], FrameError> {
        let n = self.u16()? as usize;
        if n > max {
            return Err(FrameError::Malformed("field too long"));
        }
        self.take(n)
    }

    pub fn lp32(&mut self, max: usize) -> Result<&'a [u8], FrameError> {
        let n = self.u32()? as usize;
        if n > max {
            return Err(FrameError::Malformed("field too long"));
        }
        self.take(n)
    }

    pub fn text(bytes: &'a [u8]) -> Result<&'a str, FrameError> {
        std::str::from_utf8(bytes).map_err(|_| FrameError::Malformed("text is not UTF-8"))
    }

    pub fn end(self) -> Result<(), FrameError> {
        if self.bytes.is_empty() {
            Ok(())
        } else {
            Err(FrameError::Malformed("bytes after the payload"))
        }
    }
}

pub fn put_lp8(out: &mut Vec<u8>, bytes: &[u8]) {
    debug_assert!(bytes.len() <= u8::MAX as usize);
    out.push(bytes.len() as u8);
    out.extend_from_slice(bytes);
}

pub fn put_lp16(out: &mut Vec<u8>, bytes: &[u8]) {
    debug_assert!(bytes.len() <= u16::MAX as usize);
    out.extend_from_slice(&(bytes.len() as u16).to_be_bytes());
    out.extend_from_slice(bytes);
}

pub fn put_lp32(out: &mut Vec<u8>, bytes: &[u8]) {
    out.extend_from_slice(&(bytes.len() as u32).to_be_bytes());
    out.extend_from_slice(bytes);
}

/// A call to the home control API, as it would be made over the local socket.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Request {
    /// `GET`, `POST`, `PATCH`, `DELETE`, `PUT`.
    pub method: String,
    /// Path and query, e.g. `/v1/status`.
    pub path: String,
    /// The body (JSON for the home API); empty for none.
    pub body: Vec<u8>,
}

impl Request {
    pub fn new(method: &str, path: &str, body: &[u8]) -> Request {
        Request { method: method.to_string(), path: path.to_string(), body: body.to_vec() }
    }

    /// Method and path are short printable ASCII without spaces, and the body fits.
    pub fn check(&self) -> Result<(), FrameError> {
        let method_ok = (1..=MAX_METHOD).contains(&self.method.len()) && self.method.bytes().all(|b| b.is_ascii_uppercase());
        let path_ok = (1..=MAX_PATH).contains(&self.path.len())
            && self.path.starts_with('/')
            && self.path.bytes().all(|b| b.is_ascii_graphic());
        if !method_ok {
            return Err(FrameError::Malformed("method"));
        }
        if !path_ok {
            return Err(FrameError::Malformed("path"));
        }
        if self.body.len() > MAX_REQUEST_BODY {
            return Err(FrameError::TooLarge);
        }
        Ok(())
    }

    pub fn encode(&self) -> Result<Vec<u8>, FrameError> {
        self.check()?;
        let mut out = Vec::with_capacity(8 + self.method.len() + self.path.len() + self.body.len());
        put_lp8(&mut out, self.method.as_bytes());
        put_lp16(&mut out, self.path.as_bytes());
        put_lp32(&mut out, &self.body);
        Ok(out)
    }

    pub fn decode(payload: &[u8]) -> Result<Request, FrameError> {
        let mut c = Cursor::new(payload);
        let method = Cursor::text(c.lp8(MAX_METHOD)?)?.to_string();
        let path = Cursor::text(c.lp16(MAX_PATH)?)?.to_string();
        let body = c.lp32(MAX_REQUEST_BODY)?.to_vec();
        c.end()?;
        let request = Request { method, path, body };
        request.check()?;
        Ok(request)
    }
}

/// The handler's answer: the status and body the home would have sent.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Response {
    pub status: u16,
    pub body: Vec<u8>,
}

impl Response {
    pub fn new(status: u16, body: &[u8]) -> Response {
        Response { status, body: body.to_vec() }
    }

    pub fn encode(&self) -> Result<Vec<u8>, FrameError> {
        if !(100..=599).contains(&self.status) {
            return Err(FrameError::Malformed("status"));
        }
        if self.body.len() > MAX_RESPONSE_BODY {
            return Err(FrameError::TooLarge);
        }
        let mut out = Vec::with_capacity(6 + self.body.len());
        out.extend_from_slice(&self.status.to_be_bytes());
        put_lp32(&mut out, &self.body);
        Ok(out)
    }

    pub fn decode(payload: &[u8]) -> Result<Response, FrameError> {
        let mut c = Cursor::new(payload);
        let status = c.u16()?;
        let body = c.lp32(MAX_RESPONSE_BODY)?.to_vec();
        c.end()?;
        if !(100..=599).contains(&status) {
            return Err(FrameError::Malformed("status"));
        }
        Ok(Response { status, body })
    }
}

/// Why the link (not the handler) did not serve a request.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
#[repr(u16)]
pub enum RefusalCode {
    /// More requests than the per-device rate allows. Try again later.
    RateLimited = 1,
    /// The request frame was malformed or over a limit.
    BadRequest = 2,
    /// The request did not arrive, or the handler did not answer, in time.
    Timeout = 3,
    /// Nothing is serving on this device.
    NoHandler = 4,
    /// The handler's answer could not be sent (for example, it was too large).
    Internal = 5,
    /// The device is no longer paired.
    Removed = 6,
}

impl RefusalCode {
    pub fn from_u16(n: u16) -> Option<RefusalCode> {
        use RefusalCode::*;
        [RateLimited, BadRequest, Timeout, NoHandler, Internal, Removed].into_iter().find(|c| *c as u16 == n)
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal {
    pub code: RefusalCode,
    pub text: String,
}

impl Refusal {
    pub fn new(code: RefusalCode, text: &str) -> Refusal {
        let mut text = text.to_string();
        while text.len() > MAX_REFUSAL_TEXT {
            text.pop();
        }
        Refusal { code, text }
    }

    pub fn encode(&self) -> Vec<u8> {
        let mut out = Vec::with_capacity(4 + self.text.len());
        out.extend_from_slice(&(self.code as u16).to_be_bytes());
        put_lp16(&mut out, self.text.as_bytes());
        out
    }

    pub fn decode(payload: &[u8]) -> Result<Refusal, FrameError> {
        let mut c = Cursor::new(payload);
        let code = RefusalCode::from_u16(c.u16()?).ok_or(FrameError::Malformed("refusal code"))?;
        let text = Cursor::text(c.lp16(MAX_REFUSAL_TEXT)?)?.to_string();
        c.end()?;
        Ok(Refusal { code, text })
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn header_round_trip_and_limits() {
        for kind in [FrameType::Request, FrameType::Response, FrameType::SyncMessage, FrameType::PairHello] {
            let h = header(kind, kind.max_len()).unwrap();
            assert_eq!(parse_header(&h).unwrap(), (kind, kind.max_len()));
            assert_eq!(header(kind, kind.max_len() + 1), Err(FrameError::TooLarge));
            let mut over = h;
            over[4..8].copy_from_slice(&((kind.max_len() + 1) as u32).to_be_bytes());
            assert_eq!(parse_header(&over), Err(FrameError::TooLarge));
        }
    }

    #[test]
    fn every_single_byte_change_to_a_header_is_caught_or_changes_meaning() {
        let good = header(FrameType::Request, 10).unwrap();
        for at in 0..4 {
            for bit in 0..8 {
                let mut bad = good;
                bad[at] ^= 1 << bit;
                match parse_header(&bad) {
                    Err(_) => {}
                    // The type byte may flip to another known type; never to the same meaning.
                    Ok((kind, _)) => assert!(at == 3 && kind != FrameType::Request),
                }
            }
        }
    }

    #[test]
    fn request_round_trip_and_strictness() {
        let r = Request::new("POST", "/v1/jobs?x=1", b"{\"a\":1}");
        let bytes = r.encode().unwrap();
        assert_eq!(Request::decode(&bytes).unwrap(), r);
        // Trailing byte, truncation at every point.
        let mut extra = bytes.clone();
        extra.push(0);
        assert!(Request::decode(&extra).is_err());
        for cut in 0..bytes.len() {
            assert!(Request::decode(&bytes[..cut]).is_err(), "cut at {cut}");
        }
        for bad in [Request::new("get", "/v1", b""), Request::new("GET", "v1", b""), Request::new("GET", "/v 1", b""), Request::new("GET", "/v1\r\nX: y", b"")] {
            assert!(bad.encode().is_err());
        }
        assert!(Request::new("POST", "/v1", &vec![0u8; MAX_REQUEST_BODY]).encode().is_ok());
        assert_eq!(Request::new("POST", "/v1", &vec![0u8; MAX_REQUEST_BODY + 1]).encode(), Err(FrameError::TooLarge));
    }

    #[test]
    fn response_and_refusal_round_trip() {
        let r = Response::new(200, b"ok");
        assert_eq!(Response::decode(&r.encode().unwrap()).unwrap(), r);
        assert!(Response::new(99, b"").encode().is_err());
        assert!(Response::decode(&[0, 200, 0, 0, 0, 5, 1]).is_err());
        let f = Refusal::new(RefusalCode::RateLimited, "slow down");
        assert_eq!(Refusal::decode(&f.encode()).unwrap(), f);
        assert!(Refusal::decode(&[0, 99, 0, 0]).is_err());
    }

    #[test]
    fn decode_rejects_garbage_of_every_short_length() {
        // A walk over pseudo-random inputs: decode never panics and never accepts.
        let mut state = 0x9E37_79B9_7F4A_7C15u64;
        for len in 0..200usize {
            let mut bytes = Vec::with_capacity(len);
            for _ in 0..len {
                state = state.wrapping_mul(6364136223846793005).wrapping_add(1442695040888963407);
                bytes.push((state >> 33) as u8);
            }
            assert!(decode(&bytes).is_err());
            let _ = Request::decode(&bytes);
            let _ = Response::decode(&bytes);
            let _ = Refusal::decode(&bytes);
        }
    }

    #[tokio::test]
    async fn read_frame_reads_exactly_one_and_reports_truncation() {
        let mut two = encode(FrameType::SyncMessage, b"one").unwrap();
        two.extend_from_slice(&encode(FrameType::SyncClose, b"").unwrap());
        let mut reader = &two[..];
        assert_eq!(read_frame(&mut reader).await.unwrap().payload, b"one");
        assert_eq!(read_frame_or_end(&mut reader).await.unwrap().unwrap().kind, FrameType::SyncClose);
        assert_eq!(read_frame_or_end(&mut reader).await.unwrap(), None);
        let whole = encode(FrameType::SyncMessage, b"abcdef").unwrap();
        for cut in 0..whole.len() {
            let mut reader = &whole[..cut];
            assert_eq!(read_frame(&mut reader).await, Err(FrameError::Truncated), "cut at {cut}");
        }
    }
}
