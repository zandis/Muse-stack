# Vault format, version 1

Status: implemented in `core/src/vault.rs` (work package C2, and the slot
mechanics of C10). **Not reviewed by a cryptographer.** It must have an outside
review before it holds anything real (the plan books one at gate G3). Until
then treat every claim below as the designer's intent, checked only by the
tests named in section 18.

This document is the contract. `core/conformance/rust/vault.json` pins it byte
for byte (263 vectors, written by the Rust code and re-derived independently
in Python by `tests/test_vault_vectors.py`). If the code and this text
disagree, that is a bug in one of them.

Requirements come from `docs/spec/01-architecture.md` sections 2.6, 2.10, 3.7,
5 and 6, `docs/spec/02-ux.md` sections 3 (step 5), 4.7 and 4.10, and
`docs/ecosystem-review-2026-11.md` sections 3.5 and 5.

---

## 1. What the vault promises, and what it does not

| # | Who | What they have | What they can do | What stops them, or why not |
|---|---|---|---|---|
| T1 | Thief of the disk, a copy of the store, a sync relay, the VM's disk image | Header, record ids, tags, ciphertext | Count records, see padded sizes, see which records share a kind or an indexed value, see when things change | Content, kinds, field names and values are encrypted or keyed-hashed (sections 6, 7). Sizes are bucketed. Nothing else is hidden: see section 16 |
| T2 | Someone who can write to the store | Same, plus write access | Delete or hide records; put back an older copy of a record or of everything | Changing, swapping, moving or forging a record or the header is detected (AEAD and the header MAC). Deleting, hiding and putting back old copies is **not** detected by the vault; the device-side epoch check (section 9.4) catches an old header, and sync and receipts are where the rest must be caught |
| T3 | Thief of a locked device | The device; not its PIN or biometrics | Nothing now | The slot secret is in the OS keystore. From another device: "I lost this device" removes the slot and rotates (section 9) |
| T4 | The same thief, who later gets into the device | The old master key and everything the device had copied | Read everything the device already held. **Rotation cannot take this back, and nothing can** | After rotation they cannot open the new header, read records changed or created since, or link new tags to old ones |
| T5 | Someone who finds the 24 words | The recovery key | Open the vault and every backup made for those words | Nothing, by design: the words are a full way in. Say so on the screen. "Make a new one" (section 5.5) stops the old words opening the vault, not old backups |
| T6 | Someone with only a backup file | An age file | Learn its size (so, roughly, how much is stored). Nothing else | Section 11.4 |
| T7 | Someone who guesses the home's passphrase | Key file and passphrase | Everything | Argon2id makes each guess cost 64 MiB of memory and three passes over it (section 5.2). A weak passphrase is still weak |
| T8 | The passkey provider, if passkeys are synced | Whatever the provider can do with the credential | If it can produce the PRF output, open the vault | The passkey slot is optional, off by default, and said plainly when offered (section 4.5) |
| T9 | A tampered header: slot swapped, slot added, parameters weakened, version changed | Write access to the header | Stop the vault opening (denial of service) | Every slot wrap is bound to its slot, vault, epoch and version; the whole header is under a MAC keyed by the master key; unknown versions and fields are refused (section 4) |
| T10 | Malware on an unlocked device; a memory dump while unlocked | Everything in memory | Everything | **Not defended** (architecture section 6). Keys are wiped on lock and drop, which narrows the window and nothing more |
| T11 | A future quantum computer, with today's header or backup | X25519 public keys and ciphertext | Recover the master key from a slot wrap, or open a backup | **Not defended in version 1.** Record encryption itself is 256-bit symmetric. A hybrid slot kind is a version 2 item |

Two limits deserve their own sentences because they are easy to assume away:

- **Any slot opens the whole vault.** There is one master key. A device slot
  for the always-on VM would let the VM read everything, which is more than
  architecture section 2.10 wants ("private records stay unreadable to it").
  Version 1 has no per-kind scoping. **Decided (2026-11-15): the always-on
  VM gets no vault slot by default.** It holds only what a job is handed
  for that job. The person can add a slot for it explicitly later, after
  being told plainly that a slot opens everything; a scoped slot is a later
  design (with C4 and D3).
- **If every device and the words are lost, nobody can open the vault.** There
  is no escrow and no reset.

---

## 2. Notation and building blocks

- `||` joins bytes. `u8`, `u16`, `u32`, `u64` are big-endian. `lp16(x)` is
  `u16(len(x)) || x`; `lp32(x)` is `u32(len(x)) || x`. Text is UTF-8.
  Bytes in JSON are lowercase hex; uppercase and odd lengths are refused.
- **HKDF** is HKDF-SHA-256 (RFC 5869) with 32 bytes of output.
- **HMAC** is HMAC-SHA-256.
- **AEAD** is XChaCha20-Poly1305 (draft-irtf-cfrg-xchacha-03): 32-byte key,
  24-byte nonce, output `ciphertext || tag(16)`.
- **X25519** is RFC 7748. An all-zero shared secret is refused.
- **Argon2id** is RFC 9106, version 0x13, 32 bytes of output.
- Random values come from an injected source (`VaultRng`); `OsRng` uses the
  operating system. Ids are 16 random bytes written as 32 hex characters.

**Why XChaCha20-Poly1305 and not AES-256-GCM** (the architecture allowed
either):

1. Nonces are random and chosen independently on several devices that cannot
   share a counter. With a 96-bit nonce, random choice is safe for about 2^32
   messages per key; the record-wrap key seals a head for every version of
   every record at every rotation on every device. A 192-bit nonce removes the
   question.
2. It is constant-time in plain software. Phones without AES instructions
   (older and low-end ARM) would otherwise run a table-based or bit-sliced
   AES.
3. One cipher everywhere: slot wraps, heads, bodies, the key file, the same
   construction age uses inside.

What this costs: it is not a FIPS algorithm, and Python's `cryptography` has
no XChaCha20 (section 14 shows the 12-line bridge). Neither AEAD is
key-committing; nothing here lets an attacker choose two keys for one
ciphertext, and the header MAC commits to the master key.

---

## 3. Key hierarchy

```
                 device keystore            24 words              passkey PRF
                 unwrap(blob)               (256-bit entropy)     output (32 bytes)
                       |                          |                     |
                 slot secret (32)           slot secret (32)      slot secret (32)
                       |  HKDF(salt = vault_id, info = "muse-vault/v1/slot-x25519/" || kind)
                       v                          v                     v
                 X25519 private key         X25519 private key    X25519 private key
                       \                          |                     /
                        \-------- unwraps (ECIES, section 4.2) --------/
                                                 |
                                   vault master key (VMK, 32 random bytes)
                                                 |
        HKDF(salt = vault_id, ikm = VMK, info = "muse-vault/v1/key/" || purpose)
   +-----------+---------+--------+-----------+--------+----------+------------+-------+-------+
   record-wrap   index     ref     header-mac   words    previous   export-mac   sync    store
   wraps each    blind     keyed   MAC over     seals    seals      MAC inside   sync    SQLCipher
   record's      index,    refs    the header   word     retired    the export   doc     key (app
   own key       kind tag  (HMAC)               checks   VMKs       archive      keys    shell)
        |
   per-record data key (32 random bytes, new for every version of every record)
        |
   record content
```

| Purpose string | Used for | In this package |
|---|---|---|
| `record-wrap` | AEAD key over each record head (which holds the record's own key) | yes |
| `index` | HMAC key for kind tags and blind index tags | yes |
| `ref` | HMAC key for keyed references (`Vault::keyed_ref`), used by Records (C6) to name a record without revealing it | yes |
| `header-mac` | HMAC key over the header | yes |
| `words` | AEAD key over the recovery word check values | yes |
| `previous` | AEAD key over retired master keys kept in the header | yes |
| `export-mac` | HMAC key inside the export archive | yes |
| `sync` | Root for sync document keys: `HKDF(salt = vault_id, ikm = K_sync, info = "muse-vault/v1/sync-doc/" || document id)` | derivation specified and in the vectors; nothing uses it until D3 |
| `store` | Raw key for the SQLCipher store in the app shell (section 10) | derivation specified and in the vectors; the store is a later package |

Why the vault id is the HKDF salt: the same master key bytes in two vaults
(a bug, a bad random source) still give unrelated keys, and a record cannot
be replayed into another vault.

No function returns the master key or a purpose key. `core::vault::primitives`
exposes the *functions* (keys as arguments) so the vectors can pin them.

---

## 4. The header (wrap slots)

One JSON object, stored through the store interface under the reserved
"header" entry. Nothing in it is secret. All of it is authenticated.

```json
{"format":"muse-vault-header","version":1,"vault_id":"<32 hex>","epoch":1,
 "created":1800000000,"updated":1800000003,
 "slots":[ { ...slot... }, ... ],
 "previous":[ {"epoch":1,"nonce":"<48 hex>","sealed":"<96 hex>"}, ... ],
 "mac":"<64 hex>"}
```

- `epoch` counts master keys: 1 at creation, plus 1 at each rotation.
- `created`, `updated`: Unix seconds, passed in by the caller.
- `previous`: retired master keys, section 9.3.
- Unknown fields are refused, because the MAC would not cover them. A
  `version` other than 1 is refused before anything else is looked at.
- At most 64 slots. Exactly one `recovery` slot, always: this is what makes
  "a passkey is never the only way in" a property of the file.

### 4.1 A slot

Every slot has every field; a field that does not apply is `""` (or `0`).

| Field | Meaning |
|---|---|
| `id` | 16 random bytes, hex |
| `kind` | `device`, `recovery` or `prf` |
| `label` | device: the device's name (1 to 64 bytes, unique among device slots). prf: a display name. recovery: empty |
| `created` | Unix seconds |
| `kdf` | How the slot secret is obtained and used: `keystore+hkdf-sha256`, `bip39-entropy+hkdf-sha256`, `webauthn-prf+hkdf-sha256`. Fixed per kind; anything else is refused |
| `public` | The slot's X25519 public key |
| `eph`, `nonce`, `wrapped` | The current wrap of the master key (section 4.2): ephemeral public key, 24-byte nonce, 48-byte box |
| `keystore_blob` | device: what the device's keystore returned for the slot secret. Useless without that keystore |
| `credential_id`, `prf_salt` | prf: the WebAuthn credential id and the 32-byte PRF input |
| `issuer` | recovery: id of the device slot that created these words (section 9.2) |
| `confirmed` | recovery: when the person proved they saved the words; 0 = not yet |
| `export_recipient` | recovery: the age recipient for exports (section 11) |
| `check_salt`, `check_nonce`, `check_box` | recovery: the word check values (section 5.4) |

Passphrase stretching parameters are not in the header: the passphrase
protects a device's keystore (section 5.2), not a slot, so they live in that
device's key file, authenticated there.

### 4.2 Wrapping the master key to a slot

Every slot is an **X25519 public key**, whatever its kind:

```
slot_private = HKDF(salt = vault_id, ikm = slot_secret, info = "muse-vault/v1/slot-x25519/" || kind)
slot_public  = X25519(slot_private, base point)

wrap:   eph_private = 32 random bytes;  eph = X25519(eph_private, base point)
        shared  = X25519(eph_private, slot_public)
        kek     = HKDF(salt = eph || slot_public, ikm = shared, info = "muse-vault/v1/slot-kek")
        wrapped = AEAD(kek, nonce, aad = slot_aad, plaintext = VMK)

slot_aad = "muse-vault/v1/slot" || u32(version) || vault_id(16) || u64(epoch)
           || lp32(slot id, 16 bytes) || lp32(kind) || lp32(kdf) || lp32(slot_public, 32 bytes)
```

**Why public-key slots** (a symmetric wrap under the slot secret would be
simpler): rotation has to re-wrap a *new* master key for devices that are not
present, for the recovery key whose words nobody is holding, and for a
passkey that is not being touched. With a public key in the header, any
unlocked device can do that. The alternative, keeping each slot's symmetric
key where the master key can reach it, would hand those keys to the thief
rotation is meant to lock out.

The associated data means a wrap cannot be moved to another slot, another
vault, another epoch or another format version, and the slot cannot be
re-labelled as another kind.

### 4.3 The header MAC

```
mac = HMAC(K_header-mac, transcript)

transcript = "muse-vault/v1/header" || u32(version)
   || lp32(format) || lp32(vault_id as text)
   || u64(epoch) || u64(created) || u64(updated)
   || u32(number of slots)
   || for each slot, in file order:
        lp32(id) lp32(kind) lp32(label) u64(created)
        lp32(kdf) lp32(public) lp32(eph) lp32(nonce) lp32(wrapped)
        lp32(keystore_blob) lp32(credential_id) lp32(prf_salt) lp32(issuer)
        u64(confirmed)
        lp32(export_recipient) lp32(check_salt) lp32(check_nonce) lp32(check_box)
   || u32(number of previous entries)
   || for each: u64(epoch) lp32(nonce) lp32(sealed)
```

Hex fields go in as their text, so the MAC does not depend on JSON spacing
or key order and needs no canonical JSON.

The MAC key comes from the master key, so only someone who can open the
vault can make a header. Consequences, stated plainly:

- A slot cannot be added, removed, reordered, relabelled or have its
  `keystore_blob`, `public` or passkey fields changed without the master key.
- A device that has been *removed without rotating* still knows the master
  key and could write itself back in. Removal alone is bookkeeping; locking a
  device out means rotating (section 9). The API names say so.
- An old, authentic header put back in place is not detected by the MAC.
  Section 9.4.

### 4.4 Opening

1. Parse the header; check shape and version.
2. Get the slot secret: device, `keystore.unwrap(keystore_blob)` for each
   device slot until one works; recovery, the entropy behind the typed words;
   passkey, the PRF output for the slot the person picked.
3. Derive `slot_private`; its public key must equal the slot's `public`.
4. Unwrap the master key; derive the header key; check the MAC; open
   `previous`.

Every failure in steps 2 to 4 returns the same `OpenFailed`: a wrong key, a
wrong slot and an altered header are indistinguishable to the caller. A
malformed file (step 1) is reported as such, which reveals nothing that is
not already visible in the file.

A platform keystore should refuse a blob it did not make without prompting;
if one prompts, the shell should pass only its own slot (the label says which).

### 4.5 Passkey slot (C10)

- When offering it, the shell asks `Vault::new_prf_salt` for 32 random bytes
  and uses them as the PRF input (`prf.eval.first`) when the passkey is
  created or first used. One salt per slot, stored in the slot. The browser
  or platform hashes the input with its own prefix; the vault only ever sees
  the 32-byte output.
- The output is the slot secret (`kind = "prf"`), used exactly like the
  others. It is never stored.
- `add_prf_slot` is refused until the recovery words are confirmed, and the
  header format requires a recovery slot. So a passkey is never the only way
  in: not in the file, and not in practice at the moment it is added. (If
  the person later makes new words and does not confirm them, the reminder
  on Today is what covers the gap; the vault does not remove the passkey.)
- To open: read `prf_slots()` (credential id and salt per slot), run the
  WebAuthn ceremony, pass the output to `open_with_prf(slot_id, output)`.
- Not implemented here: WebAuthn itself, and anything platform-specific.
  Risk T8 stands for synced passkeys.

---

## 5. Device keystores and the recovery key

### 5.1 `DeviceKeyStore`

```rust
pub trait DeviceKeyStore {
    fn wrap(&self, secret: &[u8]) -> Result<Vec<u8>, KeyStoreError>;
    fn unwrap(&self, blob: &[u8]) -> Result<Zeroizing<Vec<u8>>, KeyStoreError>;
}
```

The vault gives a keystore 32 random bytes (the device's slot secret), never
the master key, and keeps the returned blob in the device's slot. `unwrap`
must fail for a blob that is not its own or was altered; it may prompt for
biometrics or a PIN. Platform implementations (Keychain / Secure Enclave,
Android Keystore, DPAPI / TPM, Secret Service) come with the app shell.

A phone's PIN is the keystore's business: hardware counts the attempts. No
stretching function can make a six-digit PIN safe against someone holding
the ciphertext, so the core does not try.

`MemoryKeyStore` (tests, or a shell that fetches one 32-byte key from the
platform and lets the core wrap) and `PassphraseKeyStore` share one
construction, a deterministic authenticated wrap:

```
K_nonce = HKDF(salt = "", ikm = key, info = "muse-vault/v1/keystore/nonce")
K_wrap  = HKDF(salt = "", ikm = key, info = "muse-vault/v1/keystore/wrap")
nonce   = HMAC(K_nonce, secret)[0..24]
blob    = nonce || AEAD(K_wrap, nonce, aad = "muse-vault/v1/keystore", secret)
```

The nonce is derived from the message, so no random source is needed and a
nonce can only repeat for the same secret, which gives the same blob.

### 5.2 `PassphraseKeyStore` (the headless home and the VM)

A machine with no usable platform keystore keeps a *key file* (the caller
writes it, mode 600; the core opens no files):

```json
{"format":"muse-vault-keyfile","version":1,"kdf":"argon2id",
 "m_kib":65536,"t":3,"p":1,"salt":"<32 hex>","nonce":"<48 hex>","wrapped":"<96 hex>"}
```

```
kek        = Argon2id(NFKC(passphrase) as UTF-8, salt, m_kib, t, p)
device_key = AEAD-open(kek, nonce, aad, wrapped)
aad        = "muse-vault/v1/keyfile" || u32(version) || u32(m_kib) || u32(t) || u32(p) || salt(16)
```

`device_key` (32 random bytes) is then the key of the wrap in 5.1. Changing
the passphrase rewrites the key file and nothing else.

**Parameters.**

| | m | t | p | Why |
|---|---|---|---|---|
| Default | 64 MiB | 3 | 1 | RFC 9106 section 4, second recommendation (the one for constrained memory: 64 MiB, 3 passes). One lane, because this build computes lanes one after another (no thread pool in the core) and the smallest home is a one-core VM; extra lanes would only help an attacker with more cores |
| Lowest accepted | 19 MiB | 2 | 1 | The OWASP minimum for Argon2id. A key file asking for less is refused, so a planted file cannot make the home accept a cheap-to-guess setting |
| Highest accepted | 1 GiB | 16 | 4 | So a planted file cannot exhaust the machine |

The default was chosen to be usable on a phone as well as a desktop, in case
a shell ever uses this store on a device without a keystore: 64 MiB fits
inside mobile per-app memory limits and a 1 GB VM. Measured: 0.11 s per
derivation in a release build on the build machine (a 2.3 GHz Xeon VM); a
few times that is expected on a phone or a small VM, which **was not
measured**. If the review wants each guess to cost more, raise `t`: the
value is in the key file, so old files keep working. The parameters are inside
the associated data, so they cannot be lowered in the file without the
unwrap failing, and a wrong passphrase, an altered file and out-of-range
parameters all return the same error.

NFKC (the parked Python vault used NFC): a passphrase typed with a full-width
keyboard, common with Chinese and Japanese input methods, must give the same
key as the half-width one.

### 5.3 The recovery key

- 256 bits from the random source, shown as a **24-word BIP-39 mnemonic**:
  the entropy, then the first 8 bits of its SHA-256, cut into 24 groups of 11
  bits, each an index into a 2048-word list.
- Only the *encoding* of BIP-39 is used. There is no PBKDF2 "seed" step and
  no passphrase ("25th word"). The entropy is the slot secret directly; with
  256 bits there is nothing to stretch. **A Muse recovery key is not a wallet
  seed** and must never be typed into a wallet (UX section 4.8 keeps wallets
  on passkeys anyway).
- **Languages.** English, Traditional Chinese, Japanese and Korean. The same
  key has words in each; the vault stores no language, and words in any of
  the four open it. No word appears in two of the lists (tested), so the
  language can be worked out from the first word (`from_words_any`).
- **The lists** are the official files from
  <https://github.com/bitcoin/bips/tree/master/bip-0039>, embedded unchanged
  in `core/src/vault/wordlists/`. Their SHA-256 values are in a test:

  | List | SHA-256 |
  |---|---|
  | `english.txt` | `2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda` |
  | `chinese_traditional.txt` | `417b26b3d8500a4ae3d59717d7011952db6fc2fb84b807f3f94ac734e89c1b5f` |
  | `japanese.txt` | `2eed0aef492291e061633d7ad8117f1a2b03eb80a29d0e4e3117ac2528d05ffd` |
  | `korean.txt` | `9e95f86c167de88f450f0aaf89e87f6624a57f973c67b516e338e8e8b8897f60` |

  The BIP repository does not publish checksums of its own. What was done
  (2026-11-13): the four files were fetched from `bitcoin/bips` and from
  `trezor/python-mnemonic` (`src/mnemonic/wordlist/`), which gave identical
  bytes, and compared word for word with the lists inside the `bip39` crate
  2.2.0, which also matched. The published test vectors then tie the lists
  to the algorithm: all 96 entropy-to-words vectors for these four languages
  from `trezor/python-mnemonic/vectors.json` and the 24 from
  `bip32JP/bip32JP.github.io/test_JP_BIP39.json` pass and are in the
  conformance file. The `bip39` crate was not used: embedding four data
  files with a hash test is less to trust than another dependency, and the
  word check (5.4) needs the indices.
- **Typed input** is normalised to NFKD first, as BIP-39 requires, then
  split on white space. So an ideographic space (U+3000) works, composed and
  decomposed Hangul and kana both work, and full-width Latin letters work.
  English is also lower-cased. Traditional Chinese words are single
  characters and may be typed with or without spaces.
- **Shown output** is composed (NFC), with an ideographic space between
  Japanese words and a plain space otherwise. The official Japanese and
  Korean files are decomposed (NFKD), which some fonts and printers render
  as separate marks; reading either form back gives the same key.
- Errors name a 1-based position (`UnknownWord(7)`) or say the checksum
  failed. They never contain a word.
- Looking a word up is not constant-time (a hash map). The words are being
  typed by their owner on their own device at that moment; this is accepted.

### 5.4 "Re-enter three words", without keeping the words

Requirement: after showing the words, ask for three of them, and mark the
key confirmed; do not store the words.

Decision: **store a 4-bit keyed check value per position, encrypted.**

```
value[i]  = low 4 bits of the first byte of
            SHA-256("muse-vault/v1/word-check\0" || check_salt(16) || u8(i) || u16(word index))   i = 0..23
table     = the 24 values packed two per byte (value[0] in the high half of byte 0)
check_box = AEAD(K_words, check_nonce, aad, table)
aad       = "muse-vault/v1/word-check-box" || vault_id(16) || slot id(16) || u64(epoch)
```

- `Vault::recovery_challenge(rng)` picks three distinct positions, uniformly.
- `verify_recovery_words(language, [(position, word); 3], now)` decrypts the
  table and compares. On success it sets `confirmed` in the recovery slot.
- A wrong word passes with probability 1/16; three wrong words, 1/4096. A
  typo usually is not a list word at all and fails outright. This is a "did
  you write it down" check, not a credential, and that is enough for it.
- **What the table gives away:** to someone who already holds the master key,
  96 of the 264 bits, leaving at least 160 bits of the recovery key unknown.
  To anyone else, nothing (it is encrypted). Compare the alternatives:
  - *Derive on demand* needs the entropy kept somewhere the master key can
    reach. Then anyone who ever held the master key (the thief in T4) could
    recover the words, open the re-wrapped recovery slot after a rotation,
    and be back in. Every lost device would force new words on paper.
  - *A full hash per word* is the same thing with extra steps: 2048 guesses
    per position recovers every word.
- Word indices are the same in every language, so the check works in any of
  the four.

### 5.5 What follows for the screens

- **The words can be shown only while the app still holds the `RecoveryKey`
  it was given** (by `create`, `rotate` or `renew_recovery`). For "I'll do it
  later", `RecoveryKey::to_pending(keystore)` wraps the key with this
  device's keystore for the shell to keep **on this device only** and delete
  after confirmation.
- **Decided (2026-11-15): the words are shown once, at creation, and cannot
  be shown again.** UX section 4.10 now offers only "make a new one"
  (`renew_recovery`, or `rotate` with `RecoveryPolicy::Renew`), from an
  unlocked device. This is what 5.4 makes possible: the vault keeps no copy
  of the words.
- **Not yet in line with that decision (reported, not changed):**
  `RecoveryKey::to_pending` / `from_pending` below still let the shell keep
  a device-wrapped copy for "I'll do it later" and show the words again from
  it until they are confirmed. Under the decision, "I'll do it later" should
  lead to new words (`renew_recovery`) rather than to the old ones again;
  whether to remove the pending copy from the core is left to the vault's
  owner.
- Because the device that created the words may still hold a pending copy,
  a rotation keeps the words only if that device's slot (`issuer`) is still
  in the vault. If it is the one being removed, new words are made whatever the
  caller asked (section 9.2).

---

## 6. Records

A record is stored as two blobs. The **head** is small and holds the
record's own key; the **body** holds the content. Rotation rewrites heads and
never touches bodies.

### 6.1 Per-record keys: the choice and the reason

Each version of each record gets a fresh random 32-byte *data key*. The data
key encrypts the content and is itself sealed in the head under the
record-wrap key (derived from the master key).

The alternative is to encrypt content directly under a master-derived key
and re-encrypt everything on rotation. Rejected because:

- **Rotation after a lost device must be fast** and atomic. Re-wrapping a few
  hundred bytes per record is; re-encrypting gigabytes of health documents on
  a phone is not, and a half-finished rotation is a dangerous state.
- **Re-encrypting old content would not help anyway.** The thief already
  holds the old ciphertext and (once in) the old keys. Nothing done later on
  other devices changes what that copy can decrypt. What rotation *can* do is
  make sure nothing written or changed afterwards is readable with the old
  key, and re-wrapping achieves exactly that, because every new version gets
  a new data key sealed under the new record-wrap key.
- Deleting one record becomes destroying one small key (6.5).

One thing per-record keys leave open: a record that is *not changed* after a
rotation keeps its data key, which the thief knows. If they later obtain the
current ciphertext of that same record, they can read it. It is the content
they already had.

### 6.2 Head

```
head = "MVH1" || u8(state) || u64(version) || u32(schema) || u64(epoch) || nonce(24) || box

state    0 = live, 1 = tombstone
version  1 for a new record, +1 for each update and for the deletion
schema   the caller's number for the content's layout
epoch    which master key's record-wrap key sealed this head

box (live)      = AEAD(K_record-wrap, nonce, head_aad, head_plain)
box (tombstone) = AEAD(K_record-wrap, nonce, head_aad, "")          (16 bytes: a tag over nothing)

head_aad   = "muse-vault/v1/record-head" || vault_id(16) || record_id(16)
             || u8(state) || u64(version) || u32(schema) || u64(epoch)

head_plain = u16(L) || inner || zeros   to a multiple of 64 bytes, at least 128
inner      = data_key(32) || lp16(kind) || u16(n) || n x ( lp16(field name) || lp16(normalised value) )
```

The kind and the indexed fields are inside the head, encrypted. They are
there, and not only in the body, so that rotation can re-key the tags
(section 7) from the heads alone.

### 6.3 Body

```
body     = "MVB1" || nonce(24) || AEAD(data_key, nonce, body_aad, padded)
body_aad = "muse-vault/v1/record-body" || vault_id(16) || record_id(16)
           || u64(version) || u32(schema) || lp16(kind)
padded   = u32(len(payload)) || payload || zeros   up to the bucket (6.4)
```

Together the associated data bind **vault id, record id, kind, version and
schema** to both parts (the head carries the kind inside its plaintext, the
body in its associated data), plus state and epoch for the head. So a head
or body cannot be moved to another record, another vault or another version,
a live head cannot be turned into a tombstone or back, and the head of one
version cannot be paired with the body of another. The body has no epoch on
purpose: rotation must not need to touch it.

Limits: kind and field names are 1 to 64 of `a-z 0-9 . _ -`; at most 16
indexed fields; a normalised value at most 1024 bytes; a payload at most
64 MiB (larger things are files, a later package).

### 6.4 Padding

- **Body:** the padded length is the smallest of 256, 512, 1024, ... 65536
  bytes that fits `4 + len(payload)`; above 64 KiB, the next multiple of
  64 KiB. (The same buckets as the parked Python vault.) A stored body is
  44 bytes longer than its bucket. An observer learns "under 252 bytes",
  "about 1 KiB", "about 2 MiB"; not the length. Cost: up to 2x for small
  records, at most 64 KiB for large ones.
- **Head:** the plaintext is padded to a multiple of 64 bytes, at least 128.
  A record with no or few short indexed fields has a 193-byte head, whatever
  its kind.
- Padding is zeros, inside the AEAD, and checked on opening.

### 6.5 Updates, deletion, crypto-shredding

- **Update**: version + 1, a new data key, new nonces, new head and body.
  The kind cannot change.
- **Delete**: the head is replaced by a tombstone (version + 1) and the body
  is dropped. The tombstone is authenticated, so a deletion cannot be forged
  by someone without the key and survives sync as a fact.
- **Crypto-shredding**: the content was only ever under the data key, and
  the data key was only in the old head. Once the old head is gone, no key
  the vault has, the master key included, opens the old ciphertext (tested:
  every vault key is tried against it).
- **The limit, plainly**: this holds for copies of the *head*. A backup made
  before the deletion, or a device that has not synced yet, still has the old
  head and can still read the record until that copy is destroyed too.
  Putting a complete old copy back into the store brings the record back,
  and the vault alone cannot tell (T2).
- `purge` removes the tombstone itself, for when every device has seen it.

---

## 7. Search

- **Kind tag.** The store never sees a kind's name. It files each record
  under `hex(HMAC(K_index, "muse-vault/v1/kind\0" || kind)[0..16])`.
- **Blind index** for exact-match fields:
  `hex(HMAC(K_index, "muse-vault/v1/index\0" || lp16(kind) || lp16(field) || lp16(value))[0..16])`
  where `value` is normalised: NFKC, lowercase, NFKC again, then leading and
  trailing white space removed. `find(kind, field, value)` computes the tag,
  asks the store for matching ids, then opens each head and checks that the
  record really has that kind and that field value (so a store cannot plant
  results).
- 16 bytes (128 bits): no accidental matches, so no "decrypt and filter"
  step is needed for correctness. Truncating further to blur frequencies was
  considered and left out as cleverness with a tuning problem.
- **What an index leaks** to someone who can see the store: which records of
  a kind share a value in a field, and how many. Not the value, not the
  field name, and nothing across kinds or across vaults. **Index only what
  must be looked up, and prefer values that do not repeat** (a service name,
  an id, a hash), not, say, a diagnosis.
- **Free-text search happens on the device after decrypting.** There is no
  searchable encryption here.
- Tags are re-keyed at rotation (the index key changes with the master key),
  so old and new tags cannot be linked by someone holding only the old key.
- Normalisation depends on the Unicode version of the library doing it. The
  vectors cover 15 inputs; a second implementation on a much older Unicode
  could disagree on newly added characters. The normalised value is stored
  in the head, so tags can always be recomputed from what was stored.

---

## 8. What a store sees

```rust
pub trait VaultStore {
    fn header(&self) -> Result<Option<Vec<u8>>, StoreError>;
    fn get(&self, id: &str) -> Result<Option<StoredRecord>, StoreError>;   // kind_tag, index_tags, head, body
    fn head(&self, id: &str) -> Result<Option<Vec<u8>>, StoreError>;
    fn list(&self, kind_tag: &str) -> Result<Vec<String>, StoreError>;
    fn find(&self, kind_tag: &str, index_tag: &str) -> Result<Vec<String>, StoreError>;
    fn ids(&self) -> Result<Vec<String>, StoreError>;
    fn apply(&mut self, batch: Vec<StoreOp>) -> Result<(), StoreError>;    // all or nothing
    // provided: put(id, record), delete(id)
}
enum StoreOp { PutHeader(bytes), PutRecord { id, record }, PutHead { id, kind_tag, index_tags, head }, Delete { id } }
```

- `apply` must be atomic. Rotation is one batch: the new header and every
  re-wrapped head, or nothing.
- The store's `kind_tag` and `index_tags` columns are hints. The vault
  recomputes and checks them from the decrypted head whenever it lists or
  finds.
- `MemoryStore` is for tests. The core crate opens no files and does not
  depend on SQLite.

---

## 9. Rotation and "I lost this device"

### 9.1 What `rotate` does

One atomic batch:

1. A new random master key; `epoch + 1`.
2. Every remaining slot is re-wrapped to its public key (fresh ephemeral key
   and nonce). Absent devices, the recovery key and passkeys need not take
   part.
3. The recovery slot is kept or replaced (9.2).
4. The old master key is sealed under the new one and added to `previous`
   (9.3).
5. Every head in the store is opened with the key of its epoch and sealed
   again under the new record-wrap key; kind and index tags are recomputed
   under the new index key. Bodies are not read. A head that does not open
   is left as it is and counted (`Rotation.skipped`), so one damaged record
   cannot block a rotation.
6. New header MAC.

`lost_device(slot_id, ...)` removes the slot and does all of the above in the
same batch. `remove_device_slot` alone only edits the header (section 4.3).

**What it achieves:** the removed device's key opens no slot in the new
header; it cannot open any head written or re-wrapped since; it cannot
recognise the new tags.

**What it cannot do (T4):** anything the lost device had already copied is
readable to whoever gets into it. The "what the lost device could still
read" line in UX section 4.7 should say exactly that: everything it had
synced up to the moment it was lost.

### 9.2 Recovery words at rotation

`RecoveryPolicy::Keep` re-wraps the new master key to the same recovery
public key and re-seals the word check table; the paper stays valid.
`RecoveryPolicy::Renew` makes new words, returned in `Rotation.new_recovery`.

`Keep` is overridden, and new words made, when the device slot named by the
recovery slot's `issuer` is no longer in the header: the device that created
the words may hold a pending copy (5.5), so if it is the lost one, or the
vault was restored from a backup (which carries no device slots), the words
are no longer trusted. Both cases are tested.

### 9.3 `previous`: retired master keys

Devices sync. A device that has not yet seen a rotation may still write a
record under the old key, and that record arrives afterwards. So the header
keeps the retired master keys, each sealed under the current one:

```
sealed = AEAD(K_previous(current VMK), nonce, aad, old VMK)
aad    = "muse-vault/v1/previous" || vault_id(16) || u64(old epoch) || u64(current epoch)
```

Up to 8 are kept (the newest). A head from a retired epoch still opens; its
tags are stale until the next re-wrap. This gives the old thief nothing: they
already have the old keys and cannot open the seal.

### 9.4 Putting an old header back

The MAC cannot detect an old, authentic header put back in place (for
example, one that still lists the lost device). What limits the damage:

- Heads re-wrapped since do not open with the old key, so an old header in
  front of current records reads nothing (tested).
- `Vault::epoch()` and `check_epoch(at_least)`: each device remembers the
  highest epoch it has seen (outside the vault store, e.g. in its keystore
  entry or app settings) and refuses a lower one with `Rollback`. The shell
  must do this; the core cannot keep state.
- A complete old copy of everything is simply an old backup. See T2.

### 9.5 Pairing a new device (for D2)

```
new device:      request = Vault::enroll_device(vault_id, label, &keystore, rng)
                           // makes its slot secret, stores it in ITS keystore,
                           // returns { label, public key, keystore_blob }: nothing secret
existing device: slot_id = vault.add_device_slot(&request, rng, now)
                           // wraps the master key to that public key
new device:      Vault::open_with_device(store, &keystore)      // once the header has synced
```

"The new device receives the wrapped master key" (architecture 2.10) is
literally the new slot in the header. No key crosses the channel in either
direction. **The request must arrive over the paired, verified channel**
(the four-word check): whoever's public key is in it gets the vault.

---

## 10. Storage in the app shell (later package)

- Records go in SQLite with SQLCipher, behind `VaultStore`. This is a
  **second layer**: it hides the tags, ids, sizes and counts from someone
  holding the database file, which the vault's own encryption does not.
- SQLCipher's key is `K_store` (`purpose = "store"`), given as a raw key
  (`PRAGMA key = "x'<64 hex>'"`), so SQLCipher's own passphrase stretching is
  skipped: the key is already uniform.
- The header cannot live inside that database, because the header is needed
  to get the key. It is a small sidecar file. Making "new header + re-keyed
  database" atomic at rotation (`PRAGMA rekey`) is that package's problem
  and is not solved here.
- Nothing in the core crate depends on SQLite or SQLCipher.

---

## 11. Export and import

### 11.1 Decision: age, to an X25519 recipient derived from the recovery key

```
identity  = HKDF(salt = "", ikm = recovery entropy, info = "muse-vault/v1/export-x25519")
            used as an age X25519 identity (Bech32 "AGE-SECRET-KEY-1...")
recipient = its public key, "age1...", stored in the recovery slot as `export_recipient`
```

- **Why not age's passphrase mode with the words:** the words are a 256-bit
  key, not a passphrase; scrypt would add a second or more per open on a
  phone for nothing, and the "passphrase" would be a string whose spelling
  depends on the language and normalisation of the words.
- **Why derived from the recovery key and not the master key:** the backup
  must open in the "all devices lost" case, where the only thing left is the
  words. A recipient derived from the master key would need the header that
  is inside the file.
- **Why a recipient at all:** exporting needs only the public half, which is
  in the (authenticated) header, so an unlocked device can make a backup
  without the words being present.
- The salt is empty because the vault id is not known until the file is open.
- A separate HKDF label keeps this key apart from the recovery *slot* key.

`export(recipient)` in the architecture is narrowed to "export to the
person's own recovery key". Exporting to someone else is sharing, which is
Mandates' business, not the vault's.

### 11.2 The archive

```
age file ( to export_recipient )  wrapping:

"MVX1" || u32(len(manifest)) || manifest || header || ( head || body ) for each record || mac(32)

manifest = {"format":"muse-vault-export","version":1,"vault_id","epoch","created",
            "header_len","header_sha256",
            "records":[{"id","head_len","body_len","sha256"}, ...]}      sha256 over head || body
mac      = HMAC(K_export-mac, every byte before it)
```

- The header inside has **no device slots** (a device slot is useless on
  another device, and a backup should not carry a list of devices). The
  recovery slot, passkey slots and `previous` stay. Its MAC is recomputed.
- Records are exactly as stored, tombstones included. So the content is
  encrypted twice: per record, and by age.
- age has no sender authentication: anyone who knows the recipient can make
  a valid age file. The inner MAC and the per-record AEAD are what tie the
  contents to this vault.

### 11.3 Import

`Vault::import(archive, &recovery, empty_store, label, &keystore, rng, now)`:

1. Refuse a store that already holds anything.
2. age-decrypt with the identity derived from the words.
3. Parse; check every length and hash against the manifest.
4. Open the header's recovery slot with the words; check the header MAC,
   then the archive MAC.
5. Open **every** head and decrypt **every** body, in memory.
6. Only then: add a device slot for this device and write header and records
   in one atomic batch.

Any failure in 2 to 5 returns `ImportFailed` and writes nothing (tested with
a wrong key, bytes flipped throughout the file, truncation, extra bytes, and
a store that fails at the last step).

### 11.4 What someone with only the backup can and cannot do

Can:
- See that it is an age file for one X25519 recipient, and its size, hence
  roughly how much is stored.
- Keep it until the words leak or X25519 is broken (T5, T11).
- Destroy or withhold it.

Cannot:
- Read any content, kind, field, tag, record id, record count, device list,
  vault id or date: all of it is inside the age encryption.
- Change it without import failing: age authenticates its payload in 64 KiB
  chunks, and the inner MAC, header MAC and per-record AEAD each fail
  independently.
- Replace it with a different valid backup *of this vault*, old or forged,
  unless they have the master key: the inner MAC needs it. (An older genuine
  backup of the same vault will import. A backup is a point in time.)
- Learn anything about the words from the recipient string, short of
  breaking X25519.

A backup opens with the words **that were current when it was made**. After
"make a new one", old backups still open with the old words: destroy old
backups, or accept that.

---

## 12. Memory hygiene

- Keys live in `Secret32`, which is wiped on drop (`zeroize`), compares in
  constant time (`subtle`) and prints as `Secret32(<redacted>)`. Decrypted
  content, typed words, passphrase bytes and padded plaintext are in
  `Zeroizing` buffers.
- `Vault`, `RecoveryKey`, the keystores and `Record` have hand-written
  `Debug` that shows no key and no content. Errors are a closed enum of
  fixed text and positions; none carries content, a key or a word. The crate
  does not log.
- `lock()` drops every key. Every operation on a locked vault returns
  `Locked`. A failed unlock leaves the vault locked, even if it was open.
- Auto-lock hook: `set_auto_lock(Some(seconds), now)`, `note_activity(now)`
  from the shell on input, `lock_if_idle(now)` from a timer and when the app
  goes to the background. The core has no clock.
- MAC, public-key and word-check comparisons are constant-time.
- **Limits.** Wiping is best-effort: the compiler and allocator may have
  copied bytes (moves, reallocation of a growing buffer), the cipher and
  curve libraries keep their own state for a call, `serde_json` buffers are
  not wiped (they hold only public header and key-file JSON), and nothing
  here locks memory against swap or core dumps. The age library draws its
  own randomness from the OS and manages its own buffers during export.

---

## 13. API

| Architecture 2.6 | Here | Notes |
|---|---|---|
| | `Vault::create(store, label, &keystore, rng, now) -> (Vault, RecoveryKey)` | One device slot, one recovery slot |
| | `Vault::open_with_device(store, &keystore)`, `open_with_recovery(store, &RecoveryKey)`, `open_with_prf(store, slot_id, &prf_output)`; `locked(store)` plus `unlock_with_*` | One error for every failure |
| `put(kind, record)` | `put(kind, payload, schema, fields, rng) -> id`; `update(id, payload, schema, fields, rng) -> version` | |
| `get(id)` | `get(id) -> Record { id, kind, version, schema, payload, fields }` | |
| `search(kind, query)` | `list(kind)`, `find(kind, field, value)` | Exact match only; free text is the caller's, after decrypting |
| `delete(id)` | `delete(id, rng)`, `purge(id)` | |
| `export(recipient)` | `export(now) -> bytes`; `Vault::import(...)` | To the person's own recovery key only |
| `rotate()` | `rotate(policy, rng, now) -> Rotation`; `lost_device(slot_id, policy, rng, now)` | |
| | `Vault::enroll_device(...)`, `add_device_slot(&request, rng, now)`, `remove_device_slot(slot_id, now)`, `slots()` | |
| | `Vault::new_prf_salt(rng)`, `add_prf_slot(...)`, `prf_slots()` | C10 mechanics |
| | `RecoveryKey::words(language)`, `word(language, position)`, `from_words`, `from_words_any`, `to_pending`, `from_pending`, `export_recipient` | `recovery_words(lang)` is `RecoveryKey::words`: the vault itself cannot produce the words (5.4) |
| | `Vault::recovery_challenge(rng)`, `verify_recovery_words(language, entries, now)`, `recovery_confirmed()`, `renew_recovery(rng, now)` | |
| | `lock()`, `is_locked()`, `set_auto_lock`, `note_activity`, `lock_if_idle` | |
| | `epoch()`, `check_epoch(at_least)`, `vault_id()`, `keyed_ref(label, data)` | |

No call returns a key. "No call returns plain text off the device" is the
caller's to keep: `get` returns content to the app, which is its purpose.

---

## 14. What a Python reader needs (the headless home)

No Python consumer exists yet, so there is no Python twin. When the home
needs to read connector tokens or helper memory, a reader needs exactly
this, all of which `tests/test_vault_vectors.py` already does against the
vectors and can be lifted from:

1. **Inputs:** the key file, its passphrase, the header, and the records
   (from whatever store the home uses).
2. **Key file → device key:** NFKC the passphrase; Argon2id with the file's
   parameters (refuse any outside the accepted range); AEAD-open `wrapped`
   with the associated data of section 5.2.
3. **Device key → slot secret:** the keystore unwrap of section 5.1 on the
   slot's `keystore_blob` (find the slot by label).
4. **Slot secret → master key:** section 4.2, then **check the header MAC**
   (section 4.3) before trusting anything in the header. Open `previous`.
5. **Records:** parse the head (section 6.2), pick the record-wrap key by
   the head's epoch, AEAD-open, parse the plaintext; AEAD-open the body with
   the data key (section 6.3); remove the padding.
6. **Finding records:** kind tag and index tag (section 7).

Library support (checked with `cryptography` 50.0.2 on OpenSSL 4.0.3):

| Need | `cryptography` |
|---|---|
| HKDF-SHA-256, HMAC-SHA-256, SHA-256 | yes (and the standard library) |
| X25519 | yes |
| Argon2id | yes from version 44, on OpenSSL 3.2 or later; the project's floor is `cryptography>=42`, which would have to rise, or `argon2-cffi` be added |
| XChaCha20-Poly1305 | **no**, but it is ChaCha20-Poly1305 (present) under a subkey from HChaCha20, and HChaCha20 can be had from the ChaCha20 block function (present): encrypt 64 zero bytes with the key and the first 16 nonce bytes, subtract the constants from words 0 to 3 and the nonce words from words 12 to 15. Twelve lines, checked against the HChaCha20 test vector of draft-irtf-cfrg-xchacha-03 section 2.2.1. PyNaCl has it directly, as a new dependency |
| BIP-39 lists | read `core/src/vault/wordlists/*.txt` |
| age | not needed to read a vault; `pyrage` if the home must open backups |

A reader must be read-only at first. Writing (new records, tombstones) needs
the same care about nonces and atomic batches as the Rust code and should be
a deliberate package with its own vectors.

---

## 15. Worked example

From the conformance file, vector "scenario 1" (random stream seeded with
`muse vault scenario v1`; **test-only keys, printed here on purpose**).

A laptop creates a vault at time 1800000000. Its keystore key is
`42` x 32. The recovery words come out as

```
parent economy tent slam lunch spider ritual fox alarm toast zebra extra
master manage plunge level reason domain glow spread smoke body correct sibling
登 言 狠 浙 遇 廟 搶 衛 子 莖 韋 徑 尺 銅 忘 碳 災 席 唱 鳥 梯 總 書 乃
```

(the same key, in two of its four languages). The person confirms words 2, 9
and 23 (`economy`, `alarm`, `correct`), which sets `confirmed` to 1800000001.

The header then holds, among others, these two slots (the phone and passkey
slots added later in the scenario are left out here):

```json
{"id":"568d57fc8236f3d7d461bdaef64cf575","kind":"device","label":"laptop","created":1800000000,
 "kdf":"keystore+hkdf-sha256",
 "public":"263624d1b501d34995eeb3c71f888c57c651a5e292e8cf08cc96dba513b64630",
 "eph":"7ab8db7d49629f0d09e8643c23f8b7a5d550ad0a0c336bacbf7594e44eadda70",
 "nonce":"b969f539d3f3d1835c7d491584ec95c464b3ea3675f778b8",
 "wrapped":"db99f9603d1e047358b1b2d395bf4f137f825f765017fe0aac924db328f632c11bab0c08e07a7dbf54552e3703fa2c00",
 "keystore_blob":"2e349bbaee76a085654293002e1fba533e9510f500181682d32a85cfe7679ac87809ec94cd27954eb280181626ac7382c63ea23bab71dfd7f4f09ccc391b17580cf500ee5e22b1de",
 "credential_id":"","prf_salt":"","issuer":"","confirmed":0,"export_recipient":"","check_salt":"","check_nonce":"","check_box":""}

{"id":"4c5cb96bb66a2c28bb3877e662aa32ae","kind":"recovery","label":"","created":1800000000,
 "kdf":"bip39-entropy+hkdf-sha256",
 "public":"f2a76f5ba71f46ae879f379d5d2f2228dc4c2230a72213da6ae065a47ee9a612",
 "eph":"63b2e3be2ae94a417bde450d410c1ff22f11e7847532edb091b94617d350b865",
 "nonce":"0680ded8af903f855a706917fab3b5714fd8ca230d6db614",
 "wrapped":"ff62338d7224e65359ea84eef62c8b394d325fc2d07a4a4a40d1f4d1b83d3b9ebb56f192ac6db0b0e6b73af3b2926818",
 "keystore_blob":"","credential_id":"","prf_salt":"",
 "issuer":"568d57fc8236f3d7d461bdaef64cf575","confirmed":1800000001,
 "export_recipient":"age12e7q5er8sfjd48pysdtu7qxqqz9td7v7252kdjn5lcrjq662evqs5zk459",
 "check_salt":"992e93f8ac997e527164a290228812e9",
 "check_nonce":"4a48342918b8c28955ff900b4ba2fef43a077b878cdac34b",
 "check_box":"857bb480acc9a0640bb69d4af686c03fab1908b75a118c266b1ad89a"}
```

with `"vault_id":"a0f559c8f07959298a24031f526d2bef"`, `"epoch":1` and
`"mac":"57293e5bbe64ea66f8c39399518492298aeb4ece45a8b6fb89f2ba4cbf58ffb2"`
over a transcript whose SHA-256 is
`26cf9eec5fe0349513177800df1d15f132b8c34dcb5db987ff87bfa87adbb580`.

**Opening on the laptop:** the keystore unwraps `keystore_blob` (72 bytes:
24 of nonce, 32 of secret, 16 of tag) to the 32-byte slot secret; HKDF gives
the X25519 private key, whose public key is `2636...4630`; X25519 with `eph`
and HKDF give the wrap key; the 48-byte `wrapped` opens to the master key;
the header MAC is recomputed and compared.

**A record.** `put("memory", "喜歡喝烏龍茶", schema 1, [("topic", "Tea")])`
gives id `b3ec6267058740a4de3fd253a673840e`. The store receives:

```
kind_tag    5338b0f76ae6c77571498f6be992821d          HMAC of "memory"
index_tags  9892cde18816ed0ec8a074b8002d4846          HMAC of memory / topic / "tea"  (normalised)

head (193 bytes)
  4d564831                                           "MVH1"
  00                                                 live
  0000000000000001                                   version 1
  00000001                                           schema 1
  0000000000000001                                   epoch 1
  733193b057f3e0759ccd948349b25ff266c879f4d2500c98   nonce
  5cc2465afb25c7763b0b9ab217445ca3 ...               128 bytes of sealed key, kind and fields, + 16 of tag

body (300 bytes)
  4d564231                                           "MVB1"
  86d96b5408cc7e0bcb63d91934e748fcd7ab1aa4f91009a4   nonce
  26304bed3726e847625ac4584ffedf0a ...               256 bytes (18 bytes of content padded to the smallest bucket) + 16 of tag
```

**A deleted record** is a 65-byte head and no body:

```
4d564831 01 0000000000000002 00000001 0000000000000001  0d5b1415...faa06  0ecb082c9f77da34744ae8dcd88fabb8
"MVH1"   tombstone  version 2  schema 1   epoch 1        nonce (24)        tag over nothing
```

**Scenario 2** in the file reports the phone lost: the header's epoch
becomes 2, the phone's slot is gone, `previous` holds epoch 1, both live
heads and the tombstone are re-wrapped (their `epoch` field reads 2, their
tags change) and both bodies are byte-for-byte the same. The phone's
keystore key opens nothing in it. The same 24 words still open it.
**Scenario 3** rotates again with new words; the old words fail.

---

## 16. What remains visible

To someone holding the store (without the SQLCipher layer of section 10):
the header (how many devices and passkeys, their labels, creation times,
the epoch, whether the recovery key is confirmed, the export recipient);
how many records there are; each record's random id, padded size, version
number, schema number, epoch and whether it is a tombstone; which records
share a kind; which records of a kind share an indexed value; and, over
time, which records change and when. Device labels are in the clear in the
header: the shell should use "Laptop", not a person's name.

---

## 17. Dependencies and platforms

All pinned exactly in `core/Cargo.toml` and locked in `core/Cargo.lock`.
Versions are the ones `age` 0.12.1 itself resolves, so each primitive is
compiled once. Newer major versions of several exist (the right-hand
column); moving to them is for when `age` does, as one reviewed change.

| Crate | Pinned | Licence | Used for | Newest (2026-11-13) |
|---|---|---|---|---|
| `chacha20poly1305` | 0.10.1 | Apache-2.0 OR MIT | XChaCha20-Poly1305 | 0.11.0 |
| `hkdf` | 0.12.4 | MIT OR Apache-2.0 | HKDF | 0.13.0 |
| `hmac` | 0.12.1 | MIT OR Apache-2.0 | HMAC | 0.13.0 |
| `sha2` | 0.10 (locked 0.10.9; already a dependency) | MIT OR Apache-2.0 | SHA-256 | 0.11.1 |
| `argon2` | 0.5.3 | MIT OR Apache-2.0 | Argon2id | 0.6.0 |
| `x25519-dalek` | 2.0.1 (with `curve25519-dalek` 4.1.3) | BSD-3-Clause | Slot keys | 3.0.0 |
| `age` | 0.12.1 | MIT OR Apache-2.0 | Export archive | 0.12.1 |
| `bech32` | 0.11.1 | MIT | Encoding the age identity | 0.12.0 |
| `zeroize` | 1.9.1 | Apache-2.0 OR MIT | Wiping | 1.9.1 |
| `subtle` | 2.6.1 | BSD-3-Clause | Constant-time comparison | 2.6.1 |
| `getrandom` | 0.2.17 | MIT OR Apache-2.0 | `OsRng` | 0.4.3 |
| `unicode-normalization` | 0.1.26 | (MIT OR Apache-2.0) AND Unicode-3.0 | NFKD, NFKC, NFC | 0.1.26 |

Randomness is injected through the crate's own `VaultRng` trait rather than
`rand_core`, so the crate is not tied to one `rand_core` generation.

- The whole runtime tree is now 147 crates (before: `serde`, `serde_json`,
  `sha2` and what they need), almost all of them brought in by `age` (its
  message localisation alone accounts for dozens).
  Every licence in the tree is permissive: MIT, Apache-2.0, BSD-2/3-Clause,
  Unicode-3.0, Zlib or Unlicense, alone or as a choice (`self_cell` offers
  Apache-2.0 OR GPL-2.0-only; Apache-2.0 is taken). Checked by reading
  `cargo metadata`; `cargo deny` and `cargo audit` were **not** run (not
  installed here). `curve25519-dalek` 4.1.3 is the release that fixed the
  2024 timing advisory.
- The BIP-39 lists come from `bitcoin/bips`; the BIP text and reference
  implementation are MIT-licensed, and the lists are reproduced widely under
  MIT and CC0.
- If the size of the `age` tree is unwelcome in the phone apps, the export
  format could be produced without the `age` crate (an X25519 recipient
  stanza and the STREAM payload are about 200 lines over primitives already
  here). That would be new cryptographic code to review; it was not done.

**Mobile builds were not attempted.** What to expect:

- Everything is pure Rust; no C compiler, no OpenSSL, `#![forbid(unsafe_code)]`
  holds for this crate.
- `getrandom` 0.2 supports iOS and Android natively (`SecRandomCopyBytes`,
  `getrandom(2)` / `/dev/urandom`). It does **not** build for
  `wasm32-unknown-unknown` without its `js` feature; a web build would need
  that or an injected `VaultRng` (and `age` uses the OS source internally).
- `curve25519-dalek` picks a serial 64-bit or 32-bit backend at build time;
  32-bit ARM Android gets the 32-bit one. AVX2 detection is x86-only.
- Argon2id at 64 MiB allocates 64 MiB at once; fine on current phones, worth
  a test on low-memory Android before using `PassphraseKeyStore` there.
- No minimum Rust version is declared; only 1.97.0 was used.
- `age`'s localisation crates (`i18n-embed`, `rust-embed`) have been the
  usual source of build friction on unusual targets.

---

## 18. Tests, and what is not done

Run in `core/`: `cargo test --locked` and
`cargo clippy --all-targets --locked -- -D warnings`.

- `core/tests/vault.rs` (41 tests): round trips; one error for every wrong
  key; every byte of the header changed, with both the device and the
  recovery key; slot swap, re-pointing, removal, reordering, relabelling,
  kind change, version and epoch changes, unknown fields; every byte of a
  record's head and body; records swapped, moved, mixed across versions and
  vaults; wrong tags; rotation (bodies untouched, heads and tags changed,
  atomic on failure, unreadable heads skipped, eight retired keys kept); the
  lost-device flow including what the lost device can still read; late
  records under an old key; padding buckets; tombstones and their limits;
  export and import including a damaged archive and a wrong key; the word
  check (including the measured 1-in-16 acceptance); mnemonic errors, wrong
  language, normalisation in four languages; locking; `Debug` and stored
  bytes searched for canaries; the passphrase key file; argument checks;
  400 rounds of random damage and 120 random records through rotation,
  export and import (fixed seed).
- `core/src/vault.rs` and `mnemonic.rs` unit tests (5): no vault key opens a
  deleted record's ciphertext; lock drops keys; purpose keys differ; the
  word lists are the official files; no word is in two languages.
- `core/tests/vault_vectors.rs` (2): the committed vectors are exactly what
  the code produces; the scenario stores open with every key they name.
- `tests/test_vault_vectors.py` (13): the same vectors from Python.

The vector file lives in `core/conformance/rust/`, not directly in
`core/conformance/`, because three existing checks (the Rust conformance
test, `tests/test_conformance_export.py` and
`scripts/export_conformance.py --check`) each require that directory to
hold exactly the Python-exported files. A subdirectory keeps the two kinds
apart without editing any of them.

**Not done, and not to be assumed:**

- An outside cryptographic review. Nothing here has been audited.
- OS keystore implementations (C1, app shell); biometrics and PIN.
- The SQLite/SQLCipher store and its atomic re-key (section 10).
- WebAuthn (C10 beyond slot mechanics).
- iOS, Android and Windows/macOS builds; timing of Argon2id on real phones.
- Sync document keys in use (D3); how a header changed on two devices at
  once is merged (it is one MAC'd object, so last writer wins: D3 must
  serialise header changes or re-apply them).
- A scoped slot for the VM (section 1). By decision the VM has no slot by
  default; pairing (`core/link`, `inviter_finish`) does not yet tell the VM
  apart and adds a slot for every paired device.
- Detection of deleted, hidden or rolled-back records (T2).
- Files larger than 64 MiB; streaming.
- Post-quantum slots (T11).
- Fuzzing of the parsers; side-channel measurement; memory locking.
- Migration from the parked `muse/trust` vault (C9). That code is prior art
  only; the formats are unrelated and nothing reads the old one.

**Decided on 2026-11-15:**

1. Recovery words are shown once, at creation, and cannot be shown again;
   losing them means making a new recovery key from an unlocked device
   (section 5.5). UX section 4.10 says "make a new one".
2. The always-on VM gets no vault slot by default; the person can add one
   explicitly later (section 1).

**Decisions this document still asks the owner to confirm:**

3. `export` goes only to the person's own recovery key (section 11.1).
4. The dependency weight of `age` in the phone apps (section 17).
