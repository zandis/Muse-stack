//! Pair two devices on one machine, relays and address lookup off.
//!
//! ```text
//! cargo run --example pair -- invite            # prints the pairing code and waits
//! cargo run --example pair -- join 'muse://pair?c=...'
//! ```
//!
//! Each side prints the four check words and asks whether they match the
//! other side's (add `--yes` to accept without asking, for scripts). The
//! inviting side makes a throwaway in-memory vault; the joining side opens it
//! with its own new slot. Prototype: nothing here is stored.

use std::io::{BufRead, Write};
use std::sync::Arc;
use std::time::Duration;

use muse_core::records::SeedSigner;
use muse_core::vault::{Language, MemoryKeyStore, MemoryStore, OsRng, Vault, VaultRng};
use muse_link::pairing::{qr_version_m, AbortReason, Bootstrap, InviterContext, JoinerContext, NoRecords};
use muse_link::registry::{DeviceEntry, DeviceRegistry, MemoryRegistry, Role};
use muse_link::{Invitation, LinkConfig, LinkEndpoint, LinkKey, Pairing};

fn ask(question: &str, yes: bool) -> bool {
    if yes {
        println!("{question} [y/N] y (--yes)");
        return true;
    }
    print!("{question} [y/N] ");
    let _ = std::io::stdout().flush();
    let mut line = String::new();
    let _ = std::io::stdin().lock().read_line(&mut line);
    matches!(line.trim(), "y" | "Y" | "yes")
}

fn now() -> u64 {
    std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH).map(|d| d.as_secs()).unwrap_or(0)
}

async fn endpoint(rng: &mut OsRng) -> (LinkEndpoint, Arc<MemoryRegistry>) {
    let registry = Arc::new(MemoryRegistry::new());
    let key = LinkKey::generate(rng).expect("random bytes");
    let ep = LinkEndpoint::bind(&key, LinkConfig::offline(), registry.clone()).await.expect("bind on 127.0.0.1");
    (ep, registry)
}

fn signer(rng: &mut OsRng) -> SeedSigner {
    let mut seed = [0u8; 32];
    rng.fill(&mut seed).expect("random bytes");
    SeedSigner::new(&seed)
}

async fn invite(yes: bool) -> Result<(), String> {
    let mut rng = OsRng;
    let (ep, registry) = endpoint(&mut rng).await;
    let keystore = MemoryKeyStore::new(&mut rng).map_err(|e| e.to_string())?;
    let (mut vault, _recovery) = Vault::create(MemoryStore::new(), "this-machine", &keystore, &mut rng, now()).map_err(|e| e.to_string())?;
    let me = signer(&mut rng);
    let slot = vault.slots().into_iter().find(|s| s.kind == "device").map(|s| s.id).unwrap_or_default();
    registry.add(DeviceEntry::for_self(&me, &ep.id(), "this-machine", Role::Owner, &slot, now()).ok_or("entry")?).map_err(|e| e.to_string())?;

    let (invitation, mut session) = Pairing::invite(&ep, Duration::from_secs(300), &mut rng).map_err(|e| e.to_string())?;
    let code = invitation.to_qr_string();
    println!("Pairing code ({} characters, QR version {} at level M), valid 5 minutes:\n\n{code}\n", code.len(), qr_version_m(code.len()).unwrap_or(0));
    println!("Waiting for the other device...");
    session.accept().await.map_err(|e| e.to_string())?;
    let words = session.words(Language::English).map_err(|e| e.to_string())?;
    println!("\nCheck words: {}\n", words.join(" "));
    if !ask("Does the other device show the same four words?", yes) {
        session.reject(AbortReason::Mismatch).await;
        return Err("cancelled: the words did not match".into());
    }
    session.confirm().await.map_err(|e| e.to_string())?;
    println!("Waiting for the other device to confirm...");
    let mut records = NoRecords;
    let outcome = session
        .finish(InviterContext { vault: &mut vault, records: &mut records, rng: &mut rng, role: Role::Owner, mandate: None })
        .await
        .map_err(|e| e.to_string())?;
    println!(
        "Paired: device {} ({}) now has vault slot {}; the vault has {} device slots.",
        outcome.device.device_hex(),
        outcome.device.label,
        outcome.device.slot_id,
        vault.slots().iter().filter(|s| s.kind == "device").count()
    );
    ep.close().await;
    Ok(())
}

async fn join(code: &str, yes: bool) -> Result<(), String> {
    let mut rng = OsRng;
    let invitation = Invitation::from_qr_string(code).map_err(|e| e.to_string())?;
    let (ep, _registry) = endpoint(&mut rng).await;
    let keystore = MemoryKeyStore::new(&mut rng).map_err(|e| e.to_string())?;
    let me = signer(&mut rng);
    let mut session = Pairing::join(&ep, invitation, &mut rng).map_err(|e| e.to_string())?;
    session.connect().await.map_err(|e| e.to_string())?;
    let words = session.words(Language::English).map_err(|e| e.to_string())?;
    println!("Check words: {}\n", words.join(" "));
    if !ask("Does the other device show the same four words?", yes) {
        session.reject(AbortReason::Mismatch).await;
        return Err("cancelled: the words did not match".into());
    }
    session.confirm().await.map_err(|e| e.to_string())?;
    println!("Waiting for the other device to confirm...");
    let ks = keystore.clone();
    let mut install = move |b: &Bootstrap| -> Result<(), String> {
        let mut store = MemoryStore::new();
        store.header = Some(b.header.clone());
        Vault::open_with_device(store, &ks).map(|_| ()).map_err(|e| e.to_string())
    };
    let mut records = NoRecords;
    let outcome = session
        .finish(JoinerContext { label: "new-device", keystore: &keystore, signer: &me, install: &mut install, records: &mut records, rng: &mut rng })
        .await
        .map_err(|e| e.to_string())?;
    println!(
        "Joined vault {} at epoch {} with slot {}; opened it with this device's own key.",
        muse_core::records::to_hex(&outcome.bootstrap.vault_id),
        outcome.bootstrap.epoch,
        outcome.bootstrap.slot_id
    );
    ep.close().await;
    Ok(())
}

#[tokio::main(flavor = "multi_thread", worker_threads = 2)]
async fn main() {
    let args: Vec<String> = std::env::args().skip(1).collect();
    let yes = args.iter().any(|a| a == "--yes");
    let rest: Vec<&str> = args.iter().filter(|a| *a != "--yes").map(String::as_str).collect();
    let result = match rest.as_slice() {
        ["invite"] => invite(yes).await,
        ["join", code] => join(code, yes).await,
        _ => Err("usage: pair invite [--yes] | pair join <code> [--yes]".into()),
    };
    if let Err(e) = result {
        eprintln!("{e}");
        std::process::exit(1);
    }
}
