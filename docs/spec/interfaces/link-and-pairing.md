# Link and pairing, version 1 (prototype)

Status: **prototype** (work packages D1 and D2), implemented in the crate
`core/link/` (`muse-link`). **Not reviewed by a cryptographer.** The pairing
protocol in sections 6 to 9 must have an outside cryptographic review before
it is used with anything real. By the owner's decision the crate is **not
wired into the app or the Python home** until that review: nothing in `app/`
or `muse/` depends on it.

Contents: 1 What it is · 2 Where the code lives · 3 Identity · 4 The Link ·
5 Who may use the Link · 6 Pairing: the invitation · 7 Pairing: the protocol ·
8 The four words · 9 Key hand-over · 10 A lost device · 11 Threats ·
12 Choices and reasons · 13 What is not done · 14 Tests

---

## 1. What it is

Architecture §2.10, §3.5–3.7 and UX §4.7, made concrete:

- **Link.** Each device runs one iroh endpoint. Devices dial each other by
  public key, directly or through a relay, with no open port and no account.
  Over it a device calls the home control API (§4.1 of the architecture)
  exactly as it would over the home's Unix socket, and the later sync package
  gets an opaque, bounded message channel.
- **Pairing.** A device already in the vault shows a QR code. The new device
  scans it, both run a key exchange bound to the one-time secret in the code,
  both show the same four words, the person confirms on both, and the new
  device gets its own vault slot. No vault key crosses the channel.
- **I lost this device.** From another device: the Link refuses the lost one
  at once (open connections are closed), its vault slot is removed with a key
  rotation, and a record is written.

## 2. Where the code lives

`core/link/` is its own Cargo workspace with its own `Cargo.lock`. The pure
core (`core/Cargo.toml`) does not know it exists, so `cargo test --locked` in
`core/` neither resolves nor builds the networking stack. `muse-link` depends
on `muse-core` by path and uses only its public API (vault, records).

| Module | What |
|---|---|
| `endpoint` | `LinkEndpoint` (bind, serve, client, remove), `LinkClient` (request, sync channels, reconnect), limits |
| `frame` | the wire format of both protocols (section 4.2) |
| `registry` | `DeviceRegistry` trait, `MemoryRegistry`, link certificates |
| `handler` | `LinkHandler` trait, `Peer`, `LoopbackHandler` |
| `unix_http` | a handler that forwards to an HTTP/1.1 server on a Unix socket (the home) |
| `pairing` | invitation, Noise channel, messages, sessions, `lost_device` |

The Unix adapter builds requests and parses replies with the app's own
`muse-home-link` crate (`app/src-tauri/crates/home-link`, depended on by
path). That crate has no Tauri and no system libraries (only `serde` and
`serde_json`), so there is one implementation of the home's HTTP subset; only
the socket I/O is repeated, because that crate's is blocking.

Transport: **iroh 1.4.1**, pinned exactly (`=1.4.1`), MIT OR Apache-2.0,
MSRV 1.91, with default features minus `metrics` (`tls-ring`, `portmapper`,
`fast-apple-datapath`). Pairing: **snow 0.10.0**, pinned exactly,
Apache-2.0 OR MIT, only the primitives one pattern needs.

## 3. Identity

A device has two Ed25519 keys:

| Key | Role | Where it lives |
|---|---|---|
| **device id** | Signs the device's records (record-format §1: the device id *is* this public key). Vouches for the link key. | Ideally in hardware; only ever asked to sign. |
| **link key** | The iroh endpoint's key: what other devices dial and what QUIC/TLS authenticates. | In the memory of the networking process whenever the device is reachable. |

The device id signs `"muse/link-cert/1\0" || device_id || link_id` (the
*link certificate*). It is sent at pairing, checked against the link key the
TLS handshake authenticated, and kept in the registry entry.

**Decision: the link key is a separate key, certified by the device id.**
Reasons:

1. iroh needs the raw 32-byte secret key in process memory
   (`SecretKey::from_bytes`) to run TLS. The record-signing key should be
   able to stay in a secure element / keystore that only signs.
2. A compromise of the networking process (the part that parses input from
   the network) then cannot forge the device's records.
3. The link key can be replaced (a new certificate) without changing the
   device's identity in every record chain.

The cost is one certificate check at pairing and 64 bytes per registry entry.
Using one key for both would have been simpler; it was rejected for (1).

## 4. The Link

### 4.1 Endpoint

`LinkEndpoint::bind(&LinkKey, LinkConfig, Arc<dyn DeviceRegistry>)`.

| Setting | Values | iroh |
|---|---|---|
| `relay` | `Default` (n0's public relays), `Custom(urls)`, `None` | `RelayMode::Default` / `RelayMode::custom(..)` / `RelayMode::Disabled` |
| `discovery` | on: publish and resolve addresses through n0's DNS/pkarr service; off: addresses must be given | builder starts from `presets::Minimal` (crypto provider only), `clear_address_lookup()`, then `PkarrPublisher::n0_dns()`, `PkarrResolver::n0_dns()`, `DnsAddressLookup::n0_dns()` only when on |
| `bind` | e.g. `127.0.0.1:0` for tests | `clear_ip_transports().bind_addr(..)` |
| `proxy_from_env` | relay/lookup HTTPS through `HTTPS_PROXY` | `proxy_from_env()` |

`LinkConfig::offline()` is relays off, lookup off, loopback only: nothing
leaves the machine. Every test except one uses it.

The endpoint offers **no ALPN** until it serves (`serve(handler)` adds
`muse/link/1`) or an invitation is open (adds `muse/pair/1`); with nothing
offered, an incoming TLS handshake fails.

### 4.2 Frames

Both protocols use one framing:

```
frame = "ML" | version (1 byte, = 1) | type (1 byte) | length (u32 BE) | payload
```

The length is checked against the limit for its type **before** anything is
read or allocated; memory grows only as bytes arrive. Unknown type, another
version, short payload or trailing bytes: the stream is refused. There is no
version negotiation: version 2 will be a new ALPN.

| Type | Payload | Limit |
|---|---|---|
| `Request` 0x01 | method (lp8, `A-Z`, ≤16) · path (lp16, starts with `/`, printable, ≤2048) · body (lp32, ≤1 MiB) | |
| `Response` 0x02 | status (u16, 100–599) · body (lp32, ≤8 MiB) | |
| `Refusal` 0x03 | code (u16) · text (lp16, ≤256) | the Link, not the handler, refused |
| `SyncOpen/Accept/Message/Close` 0x04–0x07 | name (≤64, `[A-Za-z0-9._/-]`) / – / message (≤1 MiB) / – | |
| `PairHello/Noise/Data/Abort` 0x20–0x23 | section 7 | Noise message ≤65 535 |

### 4.3 Request/response

One bidirectional QUIC stream per call: the client writes one `Request`
frame and finishes; the server answers with one `Response` or `Refusal`.
`LinkClient::request(method, path, body) -> Response{status, body}`. A request
is never sent twice. Refusal codes: `RateLimited`, `BadRequest`, `Timeout`,
`NoHandler`, `Internal` (the handler's answer was over the limit), `Removed`.

### 4.4 Sync channel

`LinkClient::open_sync(name) -> SyncChannel`; `send(&[u8])`, `recv() ->
Option<Vec<u8>>`, `close()`. Ordered, at most 1 MiB a message, nothing
queued (sending waits for QUIC flow control). The serving handler gets it in
`LinkHandler::sync`. What goes in it is the sync package's business.

### 4.5 Limits (all enforced; nothing unbounded)

| What | Default |
|---|---|
| Connections in total / per device | 64 / 4 (`Busy` close code above that) |
| Bidirectional streams open per connection | 16 (QUIC flow control); unidirectional: 0 |
| Requests per device | token bucket, 20 a second, burst 40 |
| Time to receive a whole request / for the handler | 10 s / 30 s |
| Bodies | request 1 MiB, response 8 MiB, sync message 1 MiB |
| Idle connection / keep-alive | closed after 60 s / every 15 s (QUIC) |
| Client connect | 15 s per attempt, 3 attempts, exponential back-off 200 ms → 30 s with up to 25 % jitter; every refused or failed attempt counts until a request succeeds |
| Pairing connections at once | 4; hello within 10 s; each step 30 s; the person 5 min |

## 5. Who may use the Link

`DeviceRegistry` (trait) maps a link key to an entry `{device_id, link_id,
label, role, state: active|removed, cert, paired_at, slot_id}`.
`MemoryRegistry` is the in-memory implementation. Removal is final: the same
device id or link key is never added back.

On accept, for `muse/link/1`: the peer's link key (authenticated by TLS) is
looked up **before any stream is accepted**; unknown or removed → the
connection is closed with application code `0x4d01` (not paired) and no
application byte is read. The registry is asked again for every stream, so a
device removed behind the endpoint's back is refused on its next call and its
connection closed (`0x4d02`). `remove_device` / `lost_device` also close open
connections at once. The handler receives `Peer {device_id, link_id, label,
role}` from the Link; nothing in a request can claim to be someone else.

`muse/pair/1` is the one protocol an unknown key may open, and only while an
invitation is open.

**Roles.** The registry's role is the home's own: `owner`, `host`, `viewer`
(architecture §4.1). The Link adds no token scheme. The Unix adapter
(`UnixHttpHandler`) takes the home's existing device token for the peer from
a `TokenSource` (one per role, `RoleTokens`, or one per device,
`DeviceTokens`, made with `muse home token add`) and adds it as
`Authorization: Bearer` on the home side. The token never crosses the Link.
When the home's `verify(token) -> Device` gains a `verify(peer) -> Device`
twin, that lookup goes away; when device mandates replace tokens (§2.7), the
registry role becomes the mandate's caveats.

## 6. Pairing: the invitation

```
payload = version (1 byte, = 1)
        | expires (u32 BE, Unix seconds)
        | inviter link id (32 bytes)
        | one-time secret (16 bytes = 128 bits)
        | relay URL: length (1 byte, ≤120) then bytes
        | count (1 byte, ≤4) then per address: 4|6, the IP (4|16 bytes), port (u16 BE)
text    = "muse://pair?c=" + base64url(payload), no padding
```

Lifetime 30 s to 15 min (the example uses 5 min). Strict parsing: other
version, other prefix, padding, trailing bytes, more than 4 addresses or more
than 400 characters are refused.

| Invitation | Bytes | Text | QR version at level M (byte mode) |
|---|---|---|---|
| loopback only (example, relays off: one IPv4 address) | 62 | 97 | 6 |
| relay URL only (`https://use1-1.relay.n0.iroh.link./`) | 90 | 134 | 8 |
| typical: relay + one IPv4 + one IPv6 | 116 | 169 | 9 |
| largest allowed (120-byte relay URL, four IPv6) | 251 | 349 | 14 |

It holds an address and a secret that is useless after one attempt or after
expiry. **It never holds a vault key** or anything derived from one.

## 7. Pairing: the protocol

Roles: the **inviter** is a device already in the vault; the **joiner** is the
new device.

1. Inviter: `Pairing::invite(&endpoint, ttl, rng) -> (Invitation,
   InviterSession)`; the endpoint starts offering `muse/pair/1`. One open
   invitation per endpoint; a new one cancels the old.
2. Joiner: `Invitation::from_qr_string`, `Pairing::join(&endpoint,
   invitation, rng) -> JoinerSession`, `connect()`: refuses locally if
   expired; dials the inviter's link id on `muse/pair/1`. QUIC/TLS 1.3 with raw
   Ed25519 public keys authenticates **both** link keys; the joiner also
   checks the remote key equals the one in the QR code.
3. Joiner → `PairHello {version 1, invite id}` (in the clear inside TLS);
   invite id = HKDF-SHA256(salt `muse/pair/1`, secret, `invite-id`)[..8].
4. Inviter's listener compares the id in constant time. Match: the
   invitation is taken out of use **now** (single use), then expiry is checked
   (`PairAbort Expired`). No match: `PairAbort UnknownInvite`, and (default
   policy `burn_on_any_failed_attempt`) the open invitation is burned too.
5. Noise **`Noise_NNpsk0_25519_ChaChaPoly_SHA256`**, joiner initiates, psk =
   HKDF(secret, `psk`). Prologue = `muse/pair/1` · 0 · version · pattern name
   · 0 · invite id · expiry · inviter link id · joiner link id · 32 bytes of
   TLS exporter (RFC 5705, label `muse/pair/1 channel binding`, context = invite
   id). So the handshake only succeeds between two parties that know the
   secret **and** share this very TLS session **and** agree on both
   authenticated peer keys.
   - message 1 (joiner): `e` (psk mixed in first);
   - message 2 (inviter): `e, ee`, payload = commitment
     `SHA-256("muse/pair/1 commit\0" || n_i)` to the inviter's 32-byte value.
   A failed handshake: `PairAbort Handshake`; the invitation is already burned.
6. Encrypted (Noise transport keys = the session keys; messages in pieces of
   ≤60 000 bytes with a more-follows byte, ≤1 MiB a message): joiner →
   `Nonce(n_j)`; inviter → `Reveal(n_i)`; the joiner checks the commitment.
   Check value = HKDF(salt `muse/pair/1 sas`, handshake hash ‖ n_i ‖ n_j,
   `words`). State: `WordsReady`.
7. Both show `session.words(language)`. The person compares and calls
   `confirm()` on each device (sends `Confirm`), or `reject(Mismatch|Cancelled)`
   (sends `Abort`, closes, burns the invitation).
8. `finish(ctx)` on each side first waits for the **other** side's `Confirm`
   (up to 5 min). Only then does key hand-over (section 9) start.

State machine (both sides): `Waiting → WordsReady → Confirmed → Done`, any
failure → `Aborted` (final). Every call out of order (`words` before the
handshake, `confirm` twice, `finish` before `confirm`, `connect`/`accept`
twice) is `PairingError::State`. A session is single use.

Random values come from an injected `VaultRng`: one 32-byte draw per session
is expanded with HKDF into the Noise seed and the check value, so tests are
deterministic and the product uses the OS generator through the same path.

## 8. The four words

44 bits of the check value, 11 bits a word, from the BIP-39 lists already
embedded in `muse-core` (`Language::{English, ChineseTraditional, Japanese,
Korean}`), in the app's language. Both devices compute the same words from the
same session.

What they protect against, given that an attacker without the 128-bit secret
cannot complete the handshake at all (and gets one try per invitation):

- **Someone who has the secret** (shoulder-surfed or photographed QR, a second
  scanner) and gets to the inviter first. The handshake succeeds for them; the
  inviter shows words; the person's own new device shows "code already used"
  and **no words**. The rule "the same four words on both screens" is what
  stops the person from confirming. Nothing is handed over before both
  confirms.
- **A swapped QR code** pointing at the attacker's endpoint: the new device
  shows words, the inviter shows none.
- **Steering the words.** Because the inviter commits to its value before it
  sees the joiner's, and the hash covers the whole handshake, neither side can
  choose the words; matching words that some other screen happens to show
  succeeds with probability 2⁻⁴⁴, and each try burns the invitation.

What they do not protect against: a person who confirms without looking; a
compromised inviter or joiner device; anything after pairing (that is the
registry and removal). A man in the middle is not one of their jobs: the
channel binding in the prologue already makes the handshake fail (tested).

## 9. Key hand-over

After both confirmations:

1. Inviter → `VaultInfo {vault_id}`.
2. Joiner: `Vault::enroll_device(vault_id, label, keystore, rng)` makes the
   slot secret **in the joiner's own keystore** and returns `{label, X25519
   public key, keystore blob}`. Joiner → `Enroll {label, slot_public,
   keystore_blob, device_id, link_cert}`.
3. Inviter: the link certificate must verify for the link key this connection
   authenticated; neither key may be known to the registry; then
   `vault.add_device_slot(request, rng, now)` (wraps the master key to that
   public key), registry entry `active` with the chosen role. If the registry
   refuses, the slot is taken out again. Inviter → `Bootstrap {vault_id,
   epoch, slot_id, header, active devices, mandate?}`; the inviter writes a
   `device.pair` record (`failed` if the vault refused).
4. Joiner checks the bootstrap: header parses; same vault id and epoch; the
   named slot is exactly the one it asked for (kind, public key, label, blob);
   every certificate verifies; it and the inviter are listed with the keys
   this connection used. It installs the header and opens the vault with its
   own keystore (`Vault::open_with_device`), adds the entries to its
   registry, writes `device.pair`, and sends `Done`.

"The new device receives the wrapped master key" (architecture §2.10) is the
new slot in the header: ciphertext only the joiner's keystore can open. **No
secret key crosses the channel in either direction**: the messages hold a
public key, a keystore blob, a public header, public device entries and
(later) a mandate token. The happy-path test logs every plaintext either side
sends or receives and checks that none contains any 16-byte window of: every
32-byte random draw of either side (master key seed, recovery entropy, slot
secrets, keystore keys, ephemeral keys), the recovery words, both signing
seeds, both link secret keys, the invitation secret, and a record's content.

`mandate` is `None` until device mandates are issued at pairing (permission
package); the joiner then has its registry role only.

Records: `device.pair` and `device.remove`, `actor: person`, `egress: none`,
`resource` = `device:<hex device id>` (added to record-format §1.3). A failure
to record never stops or undoes the pairing; the outcome says `recorded`.

## 10. A lost device

`pairing::lost_device(&endpoint, &mut vault, device_id, policy, records, rng)`,
on any other device, in this order so a failure half-way leaves the device
shut out rather than let in:

1. registry entry → `removed` (final); open connections closed (code `0x4d02`);
   from here the Link refuses it;
2. `vault.lost_device(slot_id, policy, rng, now)`: slot removed, master key
   rotated, remaining slots re-wrapped (new recovery words if needed);
3. a `device.remove` record (`failed` if step 2 failed).

A device cannot remove itself. What the lost device had already copied stays
readable to whoever holds it (vault-format §9.1). Revoking its mandate is the
permission package's step.

## 11. Threats

| Threat | What happens | Defence |
|---|---|---|
| **Shoulder-surfed QR** (someone reads the code) | They know the secret and address | Single use: whoever connects first burns it. If the attacker is first, the person's device shows "already used" and no words, the inviter shows words: the person does not confirm (section 8). Short expiry (≤15 min). |
| **Photographed QR racing the real device** | Same as above, but deliberate and fast | Same. The real device's failure is visible; nothing is handed over before both confirms. With the default policy even a wrong guess burns the invitation, so there is one race at most. |
| **Relay operator** | Sees both endpoint ids (clients prove their key to the relay), IP addresses, timing and sizes; relays QUIC packets | Cannot read or change anything: QUIC/TLS is end to end between the two link keys; the pairing secret is inside TLS and Noise. Not hidden from it: that two keys talk. Use a custom relay or none to avoid n0. |
| **Hostile Wi-Fi / network** | Can drop, delay, reorder, inject packets; can see IPs | QUIC/TLS integrity; the peer key is checked against the QR's link id; a changed, repeated, dropped or reordered Noise message fails to decrypt and ends the session (tested). Denial of service is not prevented. |
| **A second scanner** (another device of anyone scans the same code) | Tries to join | The invitation is taken out of use by the first hello that names it; any later attempt gets `UnknownInvite` (tested: the first session continues). |
| **Replay** of a recorded pairing | Old messages sent again | Both Noise ephemerals are fresh; the invitation is single use; a replayed piece fails decryption; the TLS exporter differs per connection. |
| **Downgrade** | Make a side use something weaker | There is nothing to negotiate: one ALPN `muse/pair/1`, one Noise pattern, invitation and hello versions must equal 1, and version and pattern name are in the prologue. TLS 1.3 only (iroh). |
| **Man in the middle** (third endpoint relaying both sides) | Joiner talks to M, M to the inviter | iroh only completes a connection to the dialled key; a QR pointing at M with the real secret still fails: prologue keys and TLS exporter differ on the two legs, so the Noise handshake fails (tested). |
| **Wrong or guessed secret** | Attacker tries values | 128 bits; the first failed attempt burns the invitation (policy). Cost: anyone who knows the inviter's link id can cancel an open invitation (the person shows a new code). |
| **Unknown or removed device on the Link** | Opens `muse/link/1` | Closed before any stream is read; re-checked per stream; removal is final. |
| **Malformed / oversized frames, slow sender, flood** | Memory, time, handler | Length checked before reading; per-stream timeouts; stream, connection and rate limits (section 4.5). |
| **Expiry** | An old code | Checked by the joiner before dialling and by the inviter's listener on the hello. |

## 12. Choices and reasons

**Transport: iroh 1.4.1.** Dial by key, relays and hole-punching, no open
port, QUIC with TLS 1.3 using raw Ed25519 public keys: the TLS verifier
checks that the certificate's key is the dialled endpoint id and requires a
client certificate, so both ends are authenticated by key (iroh source
`src/tls/verifier.rs`). Default public relays (n0) are documented as free,
rate-limited (limits not published) and meant for development and testing,
with no uptime guarantee; for real use a custom relay (`RelayChoice::Custom`)
or n0's paid relays.

**Pairing key exchange: Noise `NNpsk0` (snow 0.10.0) instead of a PAKE.** A
PAKE exists to make a *low-entropy* password safe against offline guessing.
Our secret is 128 random bits in a QR code, so offline guessing is not the
threat, and a PSK handshake gives the same guarantee: only holders of the
secret complete it, and the transcript hash can be bound to both peer keys
through the prologue. The maintained Rust PAKE crate, RustCrypto `spake2`
(0.4.0 stable, 0.5.0 in pre-release, MIT OR Apache-2.0), states that it has
never had an independent audit; `snow` also states it has had no formal audit,
but Noise itself is a well-analysed framework and `NNpsk0` is one of its
standard patterns. Neither choice removes the need for our own protocol's
review. If the code were ever shortened to something a person types (a
6-digit code), this must become a PAKE.

**Commit-then-reveal for the words** so that neither side can steer them
(section 8).

**Separate link key** (section 3).

**Burn on any failed attempt** (default on): nobody gets a second try at a
code, at the cost of a cheap way to cancel open invitations.

## 13. What is not done

- Bridging the Python home to the Link (a process running `UnixHttpHandler`
  next to the home, and `verify(peer)` in the home).
- Storing the registry in the vault (a later package); `MemoryRegistry` only.
- Issuing a device mandate at pairing (`mandate: None`).
- Sync over the sync channel; the always-on VM.
- QR rendering and scanning, and the Devices screen.
- Mobile builds (iroh publishes Swift/Kotlin bindings; not tried here).
- Persisting the link key in a keystore.
- An outside cryptographic review of sections 6 to 9.

## 14. Tests

`cd core/link && cargo test` (relays and lookup off, endpoints on
127.0.0.1): unit tests for frames, invitation, messages, Noise channel
(bit flips, truncation, reorder, repeat, drop, reflection), registry and words;
`tests/link.rs` (round trip, bounded bodies, unknown peer, removed peer
mid-session and on reconnect, malformed and oversized frames, slow sender,
300 concurrent requests, per-device rate, per-device connection cap,
reconnect and back-off, sync channel); `tests/pairing.rs` (happy path with a
real vault and canaries, lost device, wrong secret, relaying third endpoint,
mismatch, joiner cancels, expiry on both sides, second joiner and replay,
inviter offline, wrong-order calls, out-of-order and forged messages, protocol
offered only while an invitation is open); `tests/unix_http.rs` (the adapter
against a tiny HTTP server on a Unix socket). `MUSE_LINK_ONLINE=1 cargo test
--test online` runs one round trip through n0's public relays.

Example: `cargo run --example pair -- invite` on one terminal, then
`cargo run --example pair -- join '<code>'` on another (add `--yes` to skip
the prompt).
