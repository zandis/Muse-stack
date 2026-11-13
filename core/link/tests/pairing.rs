//! Pairing, end to end, with endpoints on 127.0.0.1 (relays and address
//! lookup off), a real in-memory vault from `muse-core` and a deterministic
//! random source. Nothing here needs the internet.

mod common;

use std::sync::{Arc, Mutex};
use std::time::Duration;

use common::{device, manual_clock, offline, Device};
use muse_core::records::{self, HmacKey};
use muse_core::vault::{Language, MemoryKeyStore, MemoryStore, RecoveryKey, RecoveryPolicy, TestRng, Vault, VaultError, VaultRng};
use muse_link::pairing::{
    lost_device, AbortReason, Bootstrap, ChainSink, InviterContext, InviterSession, JoinerContext, JoinerSession, Msg, PlaintextLog,
    ACTION_PAIR, ACTION_REMOVE,
};
use muse_link::registry::{DeviceEntry, DeviceRegistry, DeviceState, Role};
use muse_link::{Invitation, LinkError, LoopbackHandler, Pairing, PairingError, PairingState, PAIR_ALPN};

const TTL: Duration = Duration::from_secs(120);
const NOW: u64 = 2_000_000_000;

/// A deterministic random source that remembers every 32-byte draw: those
/// are the secrets (master key seed, recovery entropy, slot secrets,
/// keystore keys, ephemeral keys), the canaries no pairing message may hold.
struct Recording {
    inner: TestRng,
    secrets: Arc<Mutex<Vec<Vec<u8>>>>,
}

impl Recording {
    fn new(seed: &[u8]) -> Recording {
        Recording { inner: TestRng::new(seed), secrets: Arc::new(Mutex::new(Vec::new())) }
    }
}

impl VaultRng for Recording {
    fn fill(&mut self, dest: &mut [u8]) -> Result<(), VaultError> {
        self.inner.fill(dest)?;
        if dest.len() == 32 {
            self.secrets.lock().unwrap().push(dest.to_vec());
        }
        Ok(())
    }
}

/// A device of the person: Link endpoint, keystore, records chain.
struct Member {
    dev: Device,
    keystore: MemoryKeyStore,
    rng: Recording,
    chain: records::MemoryStore,
    ref_key: HmacKey,
}

async fn member(n: u8, config: muse_link::LinkConfig) -> Member {
    let mut rng = Recording::new(&[n, 0x42]);
    let keystore = MemoryKeyStore::new(&mut rng).unwrap();
    Member { dev: device(n, config).await, keystore, rng, chain: records::MemoryStore::new(), ref_key: HmacKey::new(&[n; 32]).unwrap() }
}

/// The first device: makes the vault, puts a record with a canary in it, and lists itself.
fn found_vault(a: &mut Member) -> (Vault<MemoryStore>, RecoveryKey) {
    let (mut vault, recovery) = Vault::create(MemoryStore::new(), "desk", &a.keystore, &mut a.rng, NOW).unwrap();
    vault.put("note", b"CANARY-the-person's-private-note", 1, &[], &mut a.rng).unwrap();
    let slot = vault.slots().into_iter().find(|s| s.kind == "device").unwrap().id;
    let me = DeviceEntry::for_self(&a.dev.signer(), &a.dev.endpoint.id(), "desk", Role::Owner, &slot, NOW).unwrap();
    a.dev.registry.add(me).unwrap();
    (vault, recovery)
}

async fn handshake(inviter: &mut InviterSession, joiner: &mut JoinerSession) -> (Result<(), PairingError>, Result<(), PairingError>) {
    tokio::join!(inviter.accept(), joiner.connect())
}

fn contains(haystack: &[u8], needle: &[u8]) -> bool {
    needle.len() <= haystack.len() && haystack.windows(needle.len()).any(|w| w == needle)
}

fn joiner_store_from(bootstrap: &Bootstrap, records_from: &MemoryStore) -> MemoryStore {
    // The header comes from pairing; the records would come from sync (a
    // later package). Copying them here shows the new slot opens real data.
    let mut store = records_from.clone();
    store.header = Some(bootstrap.header.clone());
    store
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn happy_path_new_device_opens_the_vault_with_its_own_slot_and_no_key_crosses() {
    let mut a = member(1, offline()).await;
    let mut b = member(2, offline()).await;
    let (mut vault, recovery) = found_vault(&mut a);
    a.dev.endpoint.serve(Arc::new(LoopbackHandler));

    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    // The QR text: round trip, size, prefix; it never holds a key.
    let qr = invitation.to_qr_string();
    assert!(qr.starts_with("muse://pair?c="));
    let scanned = Invitation::from_qr_string(&qr).unwrap();
    assert_eq!(scanned, invitation);
    assert!(qr.len() <= muse_link::pairing::MAX_QR_TEXT);
    // Loopback with no relay: one IPv4 address (address lookup and relays are off).
    assert_eq!(invitation.addr.relay, None);
    let secret = invitation.to_bytes()[37..53].to_vec();

    let mut joiner = Pairing::join(&b.dev.endpoint, scanned, &mut b.rng).unwrap();
    let log: PlaintextLog = Arc::new(Mutex::new(Vec::new()));
    inviter.log_plaintext(log.clone());
    joiner.log_plaintext(log.clone());
    assert_eq!(joiner.state(), PairingState::Waiting);

    let (x, y) = handshake(&mut inviter, &mut joiner).await;
    x.unwrap();
    y.unwrap();
    assert_eq!(inviter.state(), PairingState::WordsReady);
    for language in Language::ALL {
        assert_eq!(inviter.words(language).unwrap(), joiner.words(language).unwrap());
    }
    let words = inviter.words(Language::English).unwrap();
    assert!(words.iter().all(|w| !w.is_empty()));

    inviter.confirm().await.unwrap();
    joiner.confirm().await.unwrap();
    assert_eq!(joiner.state(), PairingState::Confirmed);

    let b_keystore = b.keystore.clone();
    let a_store_snapshot = vault.store().clone();
    let mut opened: Option<Vec<u8>> = None;
    let mut install = |bootstrap: &Bootstrap| -> Result<(), String> {
        let store = joiner_store_from(bootstrap, &a_store_snapshot);
        let v = Vault::open_with_device(store, &b_keystore).map_err(|e| e.to_string())?;
        let id = v.list("note").map_err(|e| e.to_string())?.remove(0);
        opened = Some(v.get(&id).map_err(|e| e.to_string())?.payload.to_vec());
        Ok(())
    };
    let b_signer = b.dev.signer();
    let a_signer = a.dev.signer();
    let mut a_sink = ChainSink { store: &mut a.chain, signer: &a_signer, ref_key: &a.ref_key };
    let mut b_sink = ChainSink { store: &mut b.chain, signer: &b_signer, ref_key: &b.ref_key };
    let (inv, joi) = tokio::join!(
        inviter.finish(InviterContext { vault: &mut vault, records: &mut a_sink, rng: &mut a.rng, role: Role::Owner, mandate: None }),
        joiner.finish(JoinerContext {
            label: "phone",
            keystore: &b.keystore,
            signer: &b_signer,
            install: &mut install,
            records: &mut b_sink,
            rng: &mut b.rng,
        })
    );
    let inv = inv.unwrap();
    let joi = joi.unwrap();
    drop(install);
    assert_eq!(opened.as_deref(), Some(&b"CANARY-the-person's-private-note"[..]));
    assert!(inv.acknowledged && inv.recorded && joi.recorded);
    assert_eq!(inviter.state(), PairingState::Done);
    assert_eq!(joiner.state(), PairingState::Done);
    // The words still read the same after the end, and calls in the wrong order are refused.
    assert_eq!(joiner.words(Language::English).unwrap(), words);
    assert!(matches!(joiner.confirm().await, Err(PairingError::State(_))));

    // Both sides know each other now, with the keys this connection authenticated.
    assert_eq!(inv.device.device_id, b.dev.device_id());
    assert_eq!(inv.device.link_id, b.dev.endpoint.id());
    assert_eq!(joi.inviter.device_id, a.dev.device_id());
    assert_eq!(b.dev.registry.list().len(), 2);
    assert_eq!(vault.slots().iter().filter(|s| s.kind == "device").count(), 2);

    // Both wrote a `device.pair` record about the new device.
    for chain in [&a.chain, &b.chain] {
        let records = chain.records();
        assert_eq!(records.len(), 1);
        assert_eq!(records[0].action, ACTION_PAIR);
        records::verify_chain(records, None).unwrap();
    }

    // No secret crossed the channel: no 32-byte random draw of either side
    // (vault master key seed, recovery entropy, slot and keystore secrets),
    // no recovery word sequence, no link or signing key, no record content
    // and not the invitation secret is in any plaintext sent or received.
    let mut canaries: Vec<Vec<u8>> = Vec::new();
    canaries.extend(a.rng.secrets.lock().unwrap().iter().cloned());
    canaries.extend(b.rng.secrets.lock().unwrap().iter().cloned());
    assert!(canaries.len() >= 6, "{}", canaries.len());
    canaries.push(recovery.words(Language::English).as_bytes().to_vec());
    canaries.push(b"CANARY-the-person's-private-note".to_vec());
    canaries.push(secret);
    for n in [1u8, 2] {
        canaries.push(vec![n; 32]); // signing seed
        canaries.push(vec![n.wrapping_add(100); 32]); // link secret key
    }
    let log = log.lock().unwrap();
    assert!(log.len() >= 10, "{}", log.len());
    for plain in log.iter() {
        for canary in &canaries {
            // Any 16-byte window of a canary would be a leak.
            for window in canary.windows(16.min(canary.len())) {
                assert!(!contains(plain, window), "a secret is in a pairing message");
            }
        }
    }
    // The bootstrap did carry what it should: the header with B's slot.
    assert!(log.iter().any(|p| contains(p, b"muse-vault-header") || p.len() > 1000));

    // And B can now use the Link to A, as the role A gave it.
    let client = b.dev.endpoint.client(a.dev.endpoint.addr()).unwrap();
    let who = client.request("GET", "/v1/whoami", b"").await.unwrap();
    assert!(String::from_utf8(who.body).unwrap().contains("\"role\":\"owner\""));
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn lost_device_is_removed_cut_off_rotated_out_and_recorded() {
    let mut a = member(3, offline()).await;
    let mut b = member(4, offline()).await;
    let (mut vault, _recovery) = found_vault(&mut a);
    a.dev.endpoint.serve(Arc::new(LoopbackHandler));
    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    let mut joiner = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    let (x, y) = handshake(&mut inviter, &mut joiner).await;
    x.unwrap();
    y.unwrap();
    inviter.confirm().await.unwrap();
    joiner.confirm().await.unwrap();
    let mut b_store = MemoryStore::new();
    let mut install = |bootstrap: &Bootstrap| -> Result<(), String> {
        b_store.header = Some(bootstrap.header.clone());
        Ok(())
    };
    let b_signer = b.dev.signer();
    let a_signer = a.dev.signer();
    let mut a_sink = ChainSink { store: &mut a.chain, signer: &a_signer, ref_key: &a.ref_key };
    let mut b_sink = ChainSink { store: &mut b.chain, signer: &b_signer, ref_key: &b.ref_key };
    let (inv, joi) = tokio::join!(
        inviter.finish(InviterContext { vault: &mut vault, records: &mut a_sink, rng: &mut a.rng, role: Role::Viewer, mandate: None }),
        joiner.finish(JoinerContext { label: "phone", keystore: &b.keystore, signer: &b_signer, install: &mut install, records: &mut b_sink, rng: &mut b.rng })
    );
    let paired = inv.unwrap().device;
    joi.unwrap();
    drop(install);
    // B opens the vault as it was when paired.
    assert!(Vault::open_with_device(b_store.clone(), &b.keystore).is_ok());

    // B holds an open connection to A.
    let client = b.dev.endpoint.client(a.dev.endpoint.addr()).unwrap();
    assert!(client.request("GET", "/v1/whoami", b"").await.is_ok());
    assert_eq!(a.dev.endpoint.open_connections(), 1);
    let epoch = vault.epoch();

    let mut a_sink = ChainSink { store: &mut a.chain, signer: &a_signer, ref_key: &a.ref_key };
    let lost = lost_device(&a.dev.endpoint, &mut vault, &paired.device_id, RecoveryPolicy::Keep, &mut a_sink, &mut a.rng).unwrap();
    assert_eq!(lost.closed_connections, 1);
    assert!(lost.recorded);
    assert_eq!(lost.rotation.epoch, epoch + 1);
    assert_eq!(a.dev.registry.by_device(&paired.device_id).unwrap().state, DeviceState::Removed);
    assert_eq!(a.dev.endpoint.open_connections(), 0);
    assert!(!vault.slots().iter().any(|s| s.id == paired.slot_id));
    // The Link refuses B now, on the old connection and on a new one.
    assert_eq!(client.request("GET", "/v1/whoami", b"").await, Err(LinkError::NotPaired));
    let fresh = b.dev.endpoint.client(a.dev.endpoint.addr()).unwrap();
    assert_eq!(fresh.request("GET", "/v1/whoami", b"").await, Err(LinkError::NotPaired));
    // With the current header, B's slot no longer opens the vault.
    let mut current = vault.store().clone();
    current.records.clear();
    assert!(Vault::open_with_device(current, &b.keystore).is_err());
    // Records on A: device.pair then device.remove.
    let actions: Vec<String> = a.chain.records().iter().map(|r| r.action.clone()).collect();
    assert_eq!(actions, vec![ACTION_PAIR.to_string(), ACTION_REMOVE.to_string()]);
    // A device cannot remove itself, and the same device cannot be removed twice into a new state.
    let mut a_sink = ChainSink { store: &mut a.chain, signer: &a_signer, ref_key: &a.ref_key };
    assert!(matches!(
        lost_device(&a.dev.endpoint, &mut vault, &a.dev.device_id(), RecoveryPolicy::Keep, &mut a_sink, &mut a.rng),
        Err(PairingError::State(_))
    ));
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_wrong_secret_fails_and_burns_the_invitation() {
    let mut a = member(5, offline()).await;
    let mut b = member(6, offline()).await;
    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    let guess = invitation.with_secret_for_tests([0x11; 16]);
    let mut wrong = Pairing::join(&b.dev.endpoint, guess, &mut b.rng).unwrap();
    let (x, y) = handshake(&mut inviter, &mut wrong).await;
    assert_eq!(x, Err(PairingError::Burned));
    assert_eq!(y, Err(PairingError::Burned));
    assert_eq!(inviter.state(), PairingState::Aborted);
    // The right secret is no use now.
    let mut right = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    assert!(matches!(right.connect().await, Err(PairingError::Burned) | Err(PairingError::Link(LinkError::Unreachable(_)))));
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

/// A third endpoint that the joiner reaches instead of the inviter (as if the
/// QR code had been swapped for one with the relay's address and the real
/// secret) and that forwards every byte, both ways, to the real inviter.
async fn relay_between(target: muse_link::LinkAddr) -> (iroh::Endpoint, tokio::task::JoinHandle<()>) {
    let ep = iroh::Endpoint::builder(iroh::endpoint::presets::Minimal)
        .relay_mode(iroh::RelayMode::Disabled)
        .clear_ip_transports()
        .bind_addr("127.0.0.1:0".parse::<std::net::SocketAddr>().unwrap())
        .unwrap()
        .alpns(vec![PAIR_ALPN.to_vec()])
        .bind()
        .await
        .unwrap();
    let relay = ep.clone();
    let task = tokio::spawn(async move {
        let Some(incoming) = relay.accept().await else { return };
        let Ok(from_joiner) = incoming.await else { return };
        let addr = iroh::EndpointAddr::from_parts(
            iroh::PublicKey::from_bytes(&target.id).unwrap(),
            target.direct.iter().copied().map(iroh::TransportAddr::Ip),
        );
        let Ok(to_inviter) = relay.connect(addr, PAIR_ALPN).await else { return };
        let Ok((mut js, mut jr)) = from_joiner.accept_bi().await else { return };
        let Ok((mut is, mut ir)) = to_inviter.open_bi().await else { return };
        let up = async { tokio::io::copy(&mut jr, &mut is).await };
        let down = async { tokio::io::copy(&mut ir, &mut js).await };
        let _ = tokio::join!(up, down);
    });
    (ep, task)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_relaying_third_endpoint_with_the_secret_fails_channel_binding() {
    let mut a = member(7, offline()).await;
    let mut b = member(8, offline()).await;
    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    let (relay, task) = relay_between(invitation.addr.clone()).await;
    let relay_addr = muse_link::LinkAddr {
        id: *relay.id().as_bytes(),
        relay: None,
        direct: relay.addr().ip_addrs().copied().collect(),
    };
    let swapped = invitation.with_addr_for_tests(relay_addr);
    let mut joiner = Pairing::join(&b.dev.endpoint, swapped, &mut b.rng).unwrap();
    let (x, y) = handshake(&mut inviter, &mut joiner).await;
    // The hello names the right invitation, so it is taken; the handshake then
    // fails because each side binds its own TLS session and peer keys.
    assert_eq!(x, Err(PairingError::Handshake));
    assert!(matches!(y, Err(PairingError::Handshake) | Err(PairingError::Closed(_))), "{y:?}");
    assert!(inviter.words(Language::English).is_err());
    assert!(joiner.words(Language::English).is_err());
    // Burned: the real joiner cannot use it afterwards.
    let mut late = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    assert!(matches!(late.connect().await, Err(PairingError::Burned) | Err(PairingError::Link(LinkError::Unreachable(_)))));
    task.abort();
    relay.close().await;
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

/// Runs both sides to the words.
async fn to_words(a: &mut Member, b: &mut Member) -> (Invitation, InviterSession, JoinerSession) {
    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    let mut joiner = Pairing::join(&b.dev.endpoint, invitation.clone(), &mut b.rng).unwrap();
    let (x, y) = handshake(&mut inviter, &mut joiner).await;
    x.unwrap();
    y.unwrap();
    (invitation, inviter, joiner)
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_mismatch_declared_by_the_person_aborts_both_sides_and_burns_the_invitation() {
    let mut a = member(9, offline()).await;
    let mut b = member(10, offline()).await;
    let mut c = member(11, offline()).await;
    let (mut vault, _) = found_vault(&mut a);
    let slots = vault.slots().len();
    let (invitation, mut inviter, mut joiner) = to_words(&mut a, &mut b).await;
    joiner.confirm().await.unwrap();
    inviter.reject(AbortReason::Mismatch).await;
    assert_eq!(inviter.state(), PairingState::Aborted);
    assert!(matches!(inviter.confirm().await, Err(PairingError::State(_))));
    let mut install = |_: &Bootstrap| -> Result<(), String> { Ok(()) };
    let b_signer = b.dev.signer();
    let mut sink = muse_link::pairing::NoRecords;
    let result = joiner
        .finish(JoinerContext { label: "phone", keystore: &b.keystore, signer: &b_signer, install: &mut install, records: &mut sink, rng: &mut b.rng })
        .await;
    assert_eq!(result.unwrap_err(), PairingError::Aborted(AbortReason::Mismatch));
    assert_eq!(vault.slots().len(), slots);
    assert_eq!(a.dev.registry.list().len(), 1);
    // Nobody else can use the invitation.
    let mut again = Pairing::join(&c.dev.endpoint, invitation, &mut c.rng).unwrap();
    assert!(matches!(again.connect().await, Err(PairingError::Burned) | Err(PairingError::Link(LinkError::Unreachable(_)))));
    let _ = &mut vault;
    for d in [&a, &b, &c] {
        d.dev.endpoint.close().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_joiner_cancelling_aborts_the_inviter_and_nothing_is_added() {
    let mut a = member(12, offline()).await;
    let mut b = member(13, offline()).await;
    let (mut vault, _) = found_vault(&mut a);
    let slots = vault.slots().len();
    let (_, mut inviter, mut joiner) = to_words(&mut a, &mut b).await;
    inviter.confirm().await.unwrap();
    joiner.reject(AbortReason::Cancelled).await;
    let mut sink = muse_link::pairing::NoRecords;
    let result = inviter.finish(InviterContext { vault: &mut vault, records: &mut sink, rng: &mut a.rng, role: Role::Owner, mandate: None }).await;
    assert_eq!(result.unwrap_err(), PairingError::Aborted(AbortReason::Cancelled));
    assert_eq!(vault.slots().len(), slots);
    assert_eq!(a.dev.registry.list().len(), 1);
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn expiry_is_enforced_on_both_sides() {
    let (clock, now) = manual_clock(NOW);
    let mut config = offline();
    config.clock = clock;
    let mut a = member(14, config).await;
    let (ahead, _) = manual_clock(NOW + 3600);
    let mut ahead_config = offline();
    ahead_config.clock = ahead;
    let mut b = member(15, ahead_config).await;
    // Lifetimes outside 30 s to 15 min are refused.
    assert!(Pairing::invite(&a.dev.endpoint, Duration::from_secs(5), &mut a.rng).is_err());
    assert!(Pairing::invite(&a.dev.endpoint, Duration::from_secs(3600), &mut a.rng).is_err());

    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, Duration::from_secs(60), &mut a.rng).unwrap();
    assert_eq!(invitation.expires, NOW + 60);
    // The joiner's own clock is an hour ahead: it refuses before dialling (and burns nothing).
    let mut late = Pairing::join(&b.dev.endpoint, invitation.clone(), &mut b.rng).unwrap();
    assert_eq!(late.connect().await, Err(PairingError::Expired));

    // A joiner whose clock is behind still reaches the inviter, which refuses: its time is up.
    let (behind, _) = manual_clock(NOW);
    let mut slow_config = offline();
    slow_config.clock = behind;
    let mut c = member(16, slow_config).await;
    now.store(NOW + 61, std::sync::atomic::Ordering::SeqCst);
    let mut joiner = Pairing::join(&c.dev.endpoint, invitation, &mut c.rng).unwrap();
    assert_eq!(joiner.connect().await, Err(PairingError::Expired));
    assert_eq!(inviter.accept().await, Err(PairingError::Expired));
    for d in [&a, &b, &c] {
        d.dev.endpoint.close().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn a_second_joiner_is_refused_while_the_first_continues_and_a_used_invitation_cannot_be_replayed() {
    let mut a = member(17, offline()).await;
    let mut b = member(18, offline()).await;
    let mut c = member(19, offline()).await;
    let (mut vault, _) = found_vault(&mut a);
    let (invitation, mut inviter, mut joiner) = to_words(&mut a, &mut b).await;
    // C scanned the same code (over a shoulder, from a photo): too late.
    let mut second = Pairing::join(&c.dev.endpoint, invitation.clone(), &mut c.rng).unwrap();
    assert!(matches!(second.connect().await, Err(PairingError::Burned) | Err(PairingError::Link(LinkError::Unreachable(_)))));
    // B is unaffected.
    inviter.confirm().await.unwrap();
    joiner.confirm().await.unwrap();
    let mut install = |_: &Bootstrap| -> Result<(), String> { Ok(()) };
    let b_signer = b.dev.signer();
    let (mut s1, mut s2) = (muse_link::pairing::NoRecords, muse_link::pairing::NoRecords);
    let (x, y) = tokio::join!(
        inviter.finish(InviterContext { vault: &mut vault, records: &mut s1, rng: &mut a.rng, role: Role::Owner, mandate: None }),
        joiner.finish(JoinerContext { label: "phone", keystore: &b.keystore, signer: &b_signer, install: &mut install, records: &mut s2, rng: &mut b.rng })
    );
    let x = x.unwrap();
    y.unwrap();
    assert!(!x.recorded);
    // Replaying the same invitation after success: refused, by any device.
    for who in [&mut b, &mut c] {
        let mut replay = Pairing::join(&who.dev.endpoint, invitation.clone(), &mut who.rng).unwrap();
        assert!(matches!(replay.connect().await, Err(PairingError::Burned) | Err(PairingError::Link(LinkError::Unreachable(_)))));
    }
    // A session is single use too.
    assert!(matches!(joiner.connect().await, Err(PairingError::State(_))));
    for d in [&a, &b, &c] {
        d.dev.endpoint.close().await;
    }
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_inviter_being_offline_is_reported_as_unreachable() {
    let mut a = member(20, offline()).await;
    let mut config = offline();
    config.connect_timeout = Duration::from_millis(800);
    let mut b = member(21, config).await;
    let (invitation, inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    a.dev.endpoint.close().await;
    drop(inviter);
    let mut joiner = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    assert!(matches!(joiner.connect().await, Err(PairingError::Link(LinkError::Unreachable(_)))));
    assert_eq!(joiner.state(), PairingState::Aborted);
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn calls_in_the_wrong_order_are_errors() {
    let mut a = member(22, offline()).await;
    let mut b = member(23, offline()).await;
    let (mut vault, _) = found_vault(&mut a);
    let (invitation, mut inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    let mut joiner = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    assert!(matches!(inviter.words(Language::English), Err(PairingError::State(_))));
    assert!(matches!(joiner.words(Language::English), Err(PairingError::State(_))));
    assert!(matches!(inviter.confirm().await, Err(PairingError::State(_))));
    assert!(matches!(joiner.confirm().await, Err(PairingError::State(_))));
    let mut sink = muse_link::pairing::NoRecords;
    assert!(matches!(
        inviter.finish(InviterContext { vault: &mut vault, records: &mut sink, rng: &mut a.rng, role: Role::Owner, mandate: None }).await,
        Err(PairingError::State(_))
    ));
    let (x, y) = handshake(&mut inviter, &mut joiner).await;
    x.unwrap();
    y.unwrap();
    // finish before confirm; accept twice.
    let mut install = |_: &Bootstrap| -> Result<(), String> { Ok(()) };
    let b_signer = b.dev.signer();
    let mut sink2 = muse_link::pairing::NoRecords;
    assert!(matches!(
        joiner
            .finish(JoinerContext { label: "phone", keystore: &b.keystore, signer: &b_signer, install: &mut install, records: &mut sink2, rng: &mut b.rng })
            .await,
        Err(PairingError::State(_))
    ));
    assert!(matches!(inviter.accept().await, Err(PairingError::State(_))));
    inviter.confirm().await.unwrap();
    assert!(matches!(inviter.confirm().await, Err(PairingError::State(_))));
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn messages_out_of_order_or_unexpected_end_the_pairing_without_adding_anything() {
    let mut a = member(24, offline()).await;
    let mut b = member(25, offline()).await;
    let (mut vault, _) = found_vault(&mut a);
    let slots = vault.slots().len();

    // The joiner skips its confirmation and sends its keys straight away.
    let (_, mut inviter, mut joiner) = to_words(&mut a, &mut b).await;
    inviter.confirm().await.unwrap();
    joiner
        .send_for_tests(Msg::Enroll(muse_link::pairing::Enroll {
            label: "sneaky".into(),
            slot_public: [9; 32],
            keystore_blob: vec![1; 40],
            device_id: b.dev.device_id(),
            link_cert: [0; 64],
        }))
        .await
        .unwrap();
    let mut sink = muse_link::pairing::NoRecords;
    let result = inviter.finish(InviterContext { vault: &mut vault, records: &mut sink, rng: &mut a.rng, role: Role::Owner, mandate: None }).await;
    assert!(matches!(result, Err(PairingError::Protocol(_))), "{result:?}");
    // The joiner is told and stops.
    // (After the inviter's own confirmation, which was already on its way.)
    assert_eq!(joiner.recv_for_tests().await, Ok(Msg::Confirm));
    assert!(matches!(joiner.recv_for_tests().await, Ok(Msg::Abort(AbortReason::Protocol)) | Err(_)));
    assert_eq!(vault.slots().len(), slots);

    // A confirmed joiner whose keys do not vouch for its link key is refused.
    let (_, mut inviter, mut joiner) = to_words(&mut a, &mut b).await;
    inviter.confirm().await.unwrap();
    joiner.send_for_tests(Msg::Confirm).await.unwrap();
    let forged = muse_link::pairing::Enroll { label: "phone".into(), slot_public: [9; 32], keystore_blob: vec![1; 40], device_id: b.dev.device_id(), link_cert: [7; 64] };
    let attacker = async {
        // Wait for the vault id, then send keys with a bad certificate.
        let _ = joiner.recv_for_tests().await;
        joiner.send_for_tests(Msg::Enroll(forged)).await
    };
    let mut sink = muse_link::pairing::NoRecords;
    let (result, _) =
        tokio::join!(inviter.finish(InviterContext { vault: &mut vault, records: &mut sink, rng: &mut a.rng, role: Role::Owner, mandate: None }), attacker);
    assert!(matches!(result, Err(PairingError::Protocol(_))), "{result:?}");
    assert_eq!(vault.slots().len(), slots);
    assert_eq!(a.dev.registry.list().len(), 1);
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}

#[tokio::test(flavor = "multi_thread", worker_threads = 2)]
async fn the_pairing_protocol_is_only_offered_while_an_invitation_is_open() {
    let mut a = member(26, offline()).await;
    let mut config = offline();
    config.connect_timeout = Duration::from_millis(800);
    let mut b = member(27, config).await;
    let (invitation, inviter) = Pairing::invite(&a.dev.endpoint, TTL, &mut a.rng).unwrap();
    // Dropping the session closes the invitation and the protocol with it.
    drop(inviter);
    let mut joiner = Pairing::join(&b.dev.endpoint, invitation, &mut b.rng).unwrap();
    assert!(matches!(joiner.connect().await, Err(PairingError::Link(LinkError::Unreachable(_)))));
    let _ = DeviceRegistry::list(a.dev.registry.as_ref());
    a.dev.endpoint.close().await;
    b.dev.endpoint.close().await;
}
