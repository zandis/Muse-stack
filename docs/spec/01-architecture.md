# Muse v1 specification, part 1: modules and how they work together

Written 2026-11-13. Part 2 is the user experience (`02-ux.md`); part 3 is the build plan (`03-plan.md`). This specification follows `docs/ecosystem-review-2026-11.md` and replaces the module list in `docs/muse-plan.md` §10. Product positioning, the rationing rules and the helper's operating rules in the earlier documents still stand.

Four recommendations from the review were confirmed by the owner on 2026-11-13. They are still marked **(assumed)** below where they shape a module, so a later change of mind is easy to trace:

1. The router stays in Python as a local service. The Rust core holds vault, mandates, receipts, sync and the device link.
2. Phones are viewer and approver first.
3. Mandates are Biscuit tokens.
4. Chain work uses Coinbase's stack on the Base Sepolia test network, with a named exit for each piece.

Words used throughout:

| Word | Meaning |
|---|---|
| **Person** | The one human who owns an installation |
| **Device** | A desktop, phone or the person's own always-on VM, each with its own key |
| **Helper** | The agent that does work for the person |
| **Skill** | A packaged ability the helper loads (a folder with `SKILL.md` and optional tools) |
| **Allowance** | What a connected service gives for free or cheaply, with its limits and reset time |
| **Mandate** | A permission the person approved: who may do what, to what, within which limits, until when |
| **Receipt** | A signed record that something was done under a mandate |
| **Home** | The device that runs the router and the helper: the desktop, or the VM when there is one |

---

## 1. The system in one picture

```
            Person
              │  approves with a passkey
              ▼
┌─────────────────────────────── a device ───────────────────────────────┐
│  App shell (Tauri)        screens, notifications, QR pairing            │
│        │                                                                │
│  Core (Rust, in the app)                                                │
│   ├─ Vault        keys, secrets, private records                        │
│   ├─ Mandates     issue, narrow, check, revoke                          │
│   ├─ Receipts     signed chain of what was done                         │
│   ├─ Sync         the person's data, the same on every device           │
│   └─ Link         device-to-device connection, no open ports            │
└───────┬────────────────────────────────────────────────────────────────┘
        │ local socket (desktop)  or  Link (to the VM)
        ▼
┌───────────────────────── the home (desktop or VM) ──────────────────────┐
│  Helper runtime    plans jobs, loads skills, asks before writing         │
│   └─ Skill sandbox each tool in its own box, no keys inside              │
│  Router            one endpoint; rations allowances; adds the keys       │
│   └─ LiteLLM       talks to providers                                    │
│  Queue             jobs that wait for spare allowance                    │
│  Catalogue         dated facts about every service                       │
│  Connectors        LINE, files, mail, calendar (each under a mandate)    │
│  Pay               x402 client, spending-limit checks (test network)     │
└───────┬─────────────────────────────────────────────────────────────────┘
        ▼
   Providers and services (the person's own accounts)
```

Three boundaries matter and are enforced in code:

- **Key boundary.** Provider keys exist only in the router's process. Skills, tools and the app shell never see them.
- **Permission boundary.** Nothing that changes anything outside the home runs without a mandate check. The check happens where the action happens, not in the planner.
- **Plain-text boundary.** Private records are decrypted only on the person's devices. Anything relayed between devices or stored in the cloud is ciphertext.

---

## 2. Modules

Each module has one job, a small interface, and a named owner in the code. "Exists" means built and tested on `ccr-645e0264-bn082z` today.

### 2.1 Router (exists; Python)

- **Job.** One OpenAI-compatible endpoint on `127.0.0.1` for every app on the home. Chooses where each request goes, rations allowances between apps, and adds the provider key.
- **Code.** `muse/hook.py`, `ration.py`, `quota.py`, `rules.py`, `cache.py`, `policy.py`, `apps.py`, `auth.py`, `ledger.py`, on LiteLLM `1.108.5` pinned by digest and hash.
- **In.** A request with an app token and optional headers: `x-muse-task`, `x-muse-privacy`, `x-muse-allow-paid`, `x-muse-allow-oneoff`, `x-muse-handoff`.
- **Out.** The answer plus `x-muse-provider`, `x-muse-privacy`, `x-muse-app`, `x-muse-cost-usd`, `x-muse-ration`, `x-muse-task`, `x-muse-cache`, `x-muse-min-output` (the output floor raised the cap), `x-muse-answer: empty-reasoning` (a 200 with no visible text); or a refusal with `Retry-After` and an `options=` trailer (wait, hand off, pay).
- **State.** Usage ledger (SQLite, never content), blocks, app list.
- **New in v1.**
  - A local control API (section 4.1) so the app and the helper read status and change settings without shelling out to the `muse` command.
  - App tokens are minted from mandates (section 3.3), so an app's caps and its mandate cannot drift apart.
  - A floor on output tokens for reasoning models. The first real-key run showed that a small `max_tokens` on such models returns no visible text. The catalogue gains `min_output_tokens` per model, and the router raises a smaller request to that floor and says so in a header.

### 2.2 Catalogue (exists; data plus Python)

- **Job.** Dated, sourced facts about every service: limits, reset rules, privacy terms, how to connect, what it is good for.
- **Code.** `config/providers.yaml`, `config/resources/*.yaml`, `muse/catalog.py`, `muse/catalog_check.py`.
- **Rules.** Every fact has `checked_on` and `source_urls`. One entry and one key per provider. Consumer subscriptions are hand-off only.
- **New in v1.**
  - Published as one JSON file per release, so the apps and other people can read it.
  - A weekly job on the home runs `muse catalog check --pages` and puts changed pages in the maintainer's review list. Nothing is edited automatically.
  - Entries for Hugging Face (sign-in flow) and LINE (message allowance), after each is verified on the provider's own pages.

### 2.3 Helper runtime (new; Python on the home)

- **Job.** Turn a request or a schedule into a job, run it as steps, ask before any write, and report.
- **Interface.**
  - `start(job)`, `status(job_id)`, `answer(job_id, decision)`, `cancel(job_id)`, `list()`.
  - A job has: who asked, the skill, inputs, a budget (requests, tokens, money, time), the mandate it runs under, and a privacy class.
- **Behaviour.**
  1. Pick the skill. Show the budget before starting if the job is larger than a chat turn.
  2. Run steps. Each model call goes through the router with the task tier set by the skill (`light`, `everyday`, `heavy`, `frontier`).
  3. Each tool call goes through the sandbox with a token narrowed to that one call.
  4. A write produces a confirm card and waits. Reads proceed.
  5. Each skill checks its own output. A failed check retries once on a stronger allowance, then offers the three choices.
  6. Every step writes a receipt.
- **State.** Jobs and their steps in the queue database; memory in the vault (readable and editable by the person).
- **Reuse.** The `SKILL.md` folder format; MCP for tools; FastMCP for the tools Muse ships. No third-party agent framework is embedded.

### 2.4 Skill sandbox (new; on the home)

- **Job.** Run each tool so that it can do only what its token allows.
- **Design.**
  - One process per tool call, started with no environment, no provider keys, and an empty working folder plus the paths its token names.
  - Network off unless the token lists hosts.
  - On Linux and the VM: bubblewrap with Landlock. On macOS: the system sandbox profile. On Windows: a restricted token and job object. Pure-compute tools may run as WebAssembly instead.
  - A skill is installed by content hash. Before first use the person sees, in plain words, what it asks for.
- **Interface.** `run(tool, args, token) -> result | refusal`. The sandbox verifies the token itself; it does not trust the helper's word.
- **Out of scope for v1.** A public skill marketplace. Skills come from Muse's own set or from a folder the person adds.

### 2.5 Queue (exists; Python)

- **Job.** Hold work that can wait and run it on spare allowance.
- **Code.** `muse/queue.py`.
- **New in v1.** Jobs carry a mandate reference and a budget. The pacing rule already releases background work before a reset; the queue gains a "run before this allowance resets" hint so chores are scheduled against the reset clock.

### 2.6 Vault (new in the core; a Python version is parked in `muse/trust`)

- **Job.** Hold keys and private records so that only the person's devices can read them.
- **Design.**
  - A random master key per person. Each device holds it wrapped by that device's own key in the OS keystore.
  - Unlock by device biometrics or PIN. A passkey with the PRF extension is offered as a second way in where the platform supports it.
  - A printed recovery key (24 words) is created in the first-run flow. Without a device or that key, nobody can open the vault, and the app says so plainly.
  - Records are encrypted one by one (XChaCha20-Poly1305 or AES-256-GCM) with the record id and kind bound in as associated data. Search indexes are encrypted too.
  - Local storage is SQLite with SQLCipher. Exports and backups are age-encrypted archives.
- **Interface.** `put(kind, record)`, `get(id)`, `search(kind, query)`, `delete(id)`, `export(recipient)`, `rotate()`. No call returns plain text off the device.
- **What goes in it.** Mandates, receipts, helper memory, connector tokens, wallet key references, and, with the health add-on, health records.
- **What stays out of it.** Provider API keys for the router live in the home's OS keystore or `.env` (mode 600), because the router must read them unattended. The vault holds a copy for moving to a new home.

### 2.7 Mandates (new in the core) **(assumed: Biscuit)**

- **Job.** Represent every permission one way, check it wherever an action happens, and revoke it everywhere at once.
- **A mandate holds.**

  | Field | Example |
  |---|---|
  | `id` | Hash of the canonical form |
  | `issuer` | The person's root key |
  | `holder` | The helper on the VM; an app; a clinician's key |
  | `actions` | `files.read`, `mail.draft`, `pay.spend`, `llm.paid`, `health.read` |
  | `resources` | A folder; a calendar; three service hosts; a set of record kinds |
  | `limits` | 2 USDC a day; 200 requests a day; 20 pushed messages a month |
  | `purpose` | Free text for the person, plus a code where a standard has one |
  | `valid` | Not before, not after |
  | `confirm` | `each write`, `each payment over X`, or `never` |
  | `privacy` | The highest class the holder may send out |

- **Form.** A Biscuit token signed by the person's root key. Anyone holding it can make a narrower copy offline: the helper narrows its own mandate to one skill, the skill to one tool call. A narrower copy can never do more than its parent.
- **Interface.** `propose(text) -> draft`, `issue(draft, passkey) -> mandate`, `narrow(mandate, limits) -> token`, `check(token, action, resource, amount) -> allow | deny(reason)`, `revoke(id)`, `list()`.
- **Revocation.** A signed revocation list that syncs to every device and the home. A check consults the list first. Spending limits are also revoked on chain (section 2.12).
- **Standing rule from the owner's earlier answers.** The helper reads freely inside its mandates and confirms every write. `confirm: never` is allowed only for reads and for actions the person has explicitly marked routine.

### 2.8 Mandate compiler (new; the centre of the design)

- **Job.** Take one issued mandate and produce the form each enforcing system needs, all tied to the mandate's id.

  | Target | Output | Who enforces |
  |---|---|---|
  | Skills and tools | Narrowed Biscuit tokens | Sandbox |
  | Router | An app entry: priority, daily caps, paid cap, default privacy | `muse/ration.py` |
  | Connectors | The narrowest scope the service offers (for example read-only mail) | The service |
  | Payments | A spending limit on chain and matching client-side budget rules | Contract and Pay |
  | Health add-on | A FHIR Consent with purpose codes | Vault |
  | Proof | A consent receipt whose hash is anchored | Anyone, later |

- **Interface.** `compile(mandate) -> [artifact]`, `status(mandate_id) -> per-target state`, `revoke(mandate_id)` which undoes each artifact and writes a revocation receipt.
- **Invariant.** No artifact exists without a mandate, and every artifact names its mandate id. A nightly check on the home lists artifacts whose mandate is revoked or expired and removes them.

### 2.9 Receipts (new in the core; a Python version is parked)

- **Job.** A record the person can trust of everything done in their name.
- **A receipt holds.** Sequence number, time, device, mandate id, action, resource reference (a keyed hash, never the content), outcome, cost, and the hash of the previous receipt. Signed by the device that acted.
- **Chain.** One chain per device, merged in the vault by sequence and time. Every N receipts or once a day, the root of the chain is anchored outside the person's devices: on the test network through an attestation, or in a transparency log for people who never use a chain.
- **Interface.** `append(entry)`, `verify() -> ok | first broken link`, `prove(seq) -> inclusion proof`, `export(filter)`.
- **Views.** The Activity screen is a readable view of the chain. With the health add-on, the same entry is also emitted as a FHIR AuditEvent.

### 2.10 Sync and Link (new in the core)

- **Link.** Devices connect to each other by public key over iroh: direct when possible, relayed when not, always encrypted end to end. No open ports, no account. The VM is reached the same way.
- **Pairing.** The first device shows a QR code holding its address and a one-time secret. The second scans it; both show a four-word check phrase; the person confirms. The new device receives the wrapped master key.
- **Sync.** The person's data (settings, mandates, revocation list, receipts, chores, helper memory, catalogue overrides) is a set of documents merged with a CRDT library (Loro or Automerge). Each change is encrypted before it leaves the device, so a relay or the VM's disk holds ciphertext for anything marked private.
- **What the VM may read.** Only what its own mandate allows. By default: chores, the queue, and non-private settings. Private records stay unreadable to it unless the person grants a mandate for a specific job.
- **Conflicts.** Settings: last writer wins per field. Revocation list and receipts: append only. Chores: merged by id.

### 2.11 Connectors (new; on the home)

- **Job.** Reach the person's own services under a mandate.
- **v1 set.**

  | Connector | Reads | Writes (each confirmed) | Note |
  |---|---|---|---|
  | Files | Chosen folders | New files in an output folder | Local only |
  | LINE | Messages sent to the person's own bot | Replies | Replies are free; pushed messages are budgeted |
  | Mail | Search and read | Drafts only in v1 | Sending comes later |
  | Calendar | Events | Proposed events | |
  | Subscriptions | Nothing | Nothing | Opens the person's own ChatGPT, Claude or Gemini app with the question; hand-off only |

- **Interface.** Each connector is an MCP server Muse ships, run in the sandbox, holding its service token only inside its own process.

### 2.12 Pay (new; test network only) **(assumed: Base Sepolia)**

- **Job.** Let the helper pay small amounts for services within a limit the person set, and receive payment for work.
- **Parts, all reused.**
  - Wallet: a passkey smart account for the person; a separate spending key for the helper.
  - Limit: a Spend Permission (recurring allowance per period to the helper's key), created by the mandate compiler and revoked with the mandate.
  - Paying: an x402 client. A service answers 402 with a price; Pay checks the mandate (amount, host, daily total), signs, and retries.
  - Receiving: an x402 endpoint in front of a result the helper produced.
  - Proof: receipt roots anchored as attestations.
- **Rules.**
  - Muse never holds the person's key or funds. No pooled contract. Credits cannot be redeemed for money in v1.
  - Every payment over the mandate's confirm threshold is a card first.
  - Client-side checks run before the chain's, so a refusal costs nothing.
- **Interface.** `quote(url) -> price`, `pay(quote, token) -> receipt | refusal`, `limits() -> remaining today`, `revoke(mandate_id)`.
- **Exits.** A self-hostable signer for the wallet, a self-run facilitator for x402, and receipt verification that does not need the chain.

### 2.13 Credits (new; a ledger, no token)

- **Job.** Let people exchange spare capacity. The first job type is spare local compute.
- **Design.** A credit is created only by a pair of matching receipts: the provider's "I did this job" and the requester's "I received it". Balances halve every 30 days. A small overdraft grows with history. All of this is signed ledger entries between the two parties, synced like other data, with periodic roots anchored.
- **Not in v1.** Any token, any redemption for money, any exchange between strangers without a standing mandate.

### 2.14 Health add-on (parked; specified so the core does not block it)

- **Job.** Let a person keep their own health records in the vault and share them on their own terms.
- **Design.**
  - Records are FHIR R4 resources (TW Core, International Patient Summary, mCODE for oncology) stored as vault records.
  - Ingestion, first route: a photo or PDF of the person's own record copy, read on the device, with every extracted value marked draft until the person confirms it against the source image.
  - Sharing: a SMART Health Link, encrypted, with a passcode and an expiry. Muse relays ciphertext only.
  - A health mandate compiles to a FHIR Consent; its receipt follows ISO/IEC TS 27560; its purpose uses GA4GH DUO codes.
- **Gates before any real use.** A regulator's view on "explain my report"; a Taiwan entity or hospital partner; a lawyer's review of consent wording. No real patient data until then.

### 2.15 App shell (new; Tauri)

- **Job.** The screens, notifications, pairing, and the passkey prompt. It contains the core and talks to the home.
- **Desktop.** Also starts and stops the home services, shows a menu-bar gauge, and hosts the page that gives other apps the endpoint.
- **Phone.** **(assumed)** Viewer and approver: status, approvals, asking the helper, pairing, recovery. It runs no chores by itself. Work happens on the home.

---

## 3. How the modules work together

### 3.1 A chat question

```
App → Helper.start(ask)
Helper → Router        POST /v1/chat/completions  (x-muse-task: everyday, app token)
Router                 privacy → rules → rationing → pick allowance → add key
Router → Provider      request
Router → Helper        answer + headers (who answered, cost, cache)
Helper → Receipts      append(read-only step)
Helper → App           answer + one line: "Used your Groq allowance. Nothing spent."
```

If the router refuses, the trailer lists what is possible now (wait until a time, hand off, pay an estimated amount) and the app shows exactly those choices.

### 3.2 A chore that writes something

```
Queue → Helper         job due (weekly digest), mandate M
Helper → Mandates      narrow(M → skill "digest")
Helper → Sandbox       run(fetch feeds, token)        allowed: listed hosts only
Helper → Router        summarise (x-muse-task: light, background priority)
Router                 held if the allowance is behind its pacing line → Queue retries after Retry-After
Helper → App           confirm card: "Save digest to Documents/Muse?"   (M says confirm each write)
Person → App           approve
Helper → Sandbox       run(write file, token narrowed to that path)
Helper → Receipts      append(write, resource hash, cost 0)
```

### 3.3 Granting a mandate

```
Person → App           "Let my helper pay up to 2 USDC a day for these three services"
App → Mandates         propose(text) → draft shown in plain words
Person → App           approve with passkey
Mandates               issue → mandate M (signed)
Compiler               compile(M):
                         Router   → app entry "helper-pay" with caps
                         Pay      → Spend Permission on chain + client budget
                         Receipts → consent receipt, hash queued for anchoring
Sync                   M and the artifacts' status reach every device
App                    Permissions screen shows M with a green mark per target
```

Revoking runs the same path backwards and ends with a revocation receipt. The Permissions screen shows each target turning off.

### 3.4 Paying for a service

```
Helper → Pay           quote(url)                     service answers 402 with a price
Pay → Mandates         check(token, pay.spend, host, amount)
Pay                    under the confirm threshold? yes → sign; no → confirm card first
Pay → Service          retry with payment
Pay → Receipts         append(payment, amount, service, mandate id)
```

### 3.5 Adding the always-on VM

```
App                    "Add an always-on helper" → opens Oracle's stack page, prefilled
Person                 approves in Oracle's console
VM (first boot)        installs home services, creates its device key, shows nothing publicly
App                    shows a QR code; the VM's setup page (reached over the Link) scans or accepts the code
Both                   four-word check phrase; person confirms
Mandates               issue VM mandate (run chores, use these allowances, no private records)
Sync                   VM receives chores, queue, router settings; becomes the home
Desktop                stops its own router; points at the VM over the Link
```

Provider keys move to the VM inside the encrypted pairing channel, never through a third party.

### 3.6 Approving from the phone or LINE

```
Helper (on the home)   needs a decision → writes a pending approval (synced)
Phone                  notification → card with the plain-language action and its mandate
Person                 approve with biometrics
Core (phone)           narrow(M → this one action, expires in 5 minutes) → token synced back
Helper                 proceeds with that token; receipt names the approving device
```

In LINE the helper replies in the person's own chat with the same card. A tap opens the app to approve; LINE itself never carries a token.

### 3.7 A new device, and a lost one

- **New.** Pair by QR (section 2.10). The new device gets the wrapped master key and its own device mandate.
- **Lost.** From any other device: revoke the lost device's mandate, rotate the master key, re-wrap for the remaining devices. Its receipts stay valid as history.
- **All lost.** The recovery key opens the last backup on a new device. Without it, the vault cannot be opened by anyone.

---

## 4. Interfaces between processes

### 4.1 Home control API (built: v1)

Local only: a Unix socket on the home (`MUSE_HOME/home.sock`, mode 0600, same user only), `127.0.0.1` on Windows, or carried over the Link. JSON, versioned under `/v1`. The contract is `docs/spec/interfaces/home-api.v1.yaml`; a test keeps it equal to the server's route table. Code: `muse/home/`.

Authenticated by a device token sent as `Authorization: Bearer`. In v1 the token is a random secret made by `muse home token add` and stored on the home only as a hash. The check sits behind one interface (`verify(token) -> Device`), so a token narrowed from the device's mandate (section 2.7) replaces it without changing any call.

| Call | Purpose |
|---|---|
| `GET /v1/status` | The snapshot the dashboard uses today: allowances, apps, queue, paid total, paused |
| `GET /v1/catalogue` | The catalogue with the person's connection state |
| `POST /v1/connect/{provider}` | Start a guided connection; returns the steps (the client opens the pages) |
| `POST /v1/keys/{provider}` | Store a key (sent inside the Link's encrypted channel); the only call that accepts one |
| `GET /v1/jobs`, `POST /v1/jobs`, `GET /v1/jobs/{id}`, `DELETE /v1/jobs/{id}` | Jobs. Prompts are accepted and never returned |
| `GET /v1/cards`, `GET /v1/cards/{id}`, `POST /v1/cards/{id}/answer` | Approvals: the cards waiting for the person, and their answers |
| `POST /v1/cards` | A device asks the person a question (added 13 November 2026 for the Muse tools server, part 5): one waiting card, by the card store's rules; at most 20 waiting per device; the asking device cannot answer it |
| `GET /v1/apps`, `POST /v1/apps`, `PATCH /v1/apps/{name}`, `DELETE /v1/apps/{name}`, `POST /v1/apps/{name}/rotate` | The apps that share the router; a new app token is returned once |
| `POST /v1/pause`, `POST /v1/resume` | The one switch |
| `GET /v1/mandates/artifacts` | Per-target state from the compiler (answers 501 until the compiler exists) |
| `GET /v1/receipts?since=` | Receipts written on the home (answers 501 until the records module exists) |

Approvals are answered on the card, not on the job (`POST /v1/cards/{id}/answer` replaces the earlier `POST /jobs/{id}/answer`): one job can ask several questions, and a card can come from something that is not a job, such as a permission request. A card's shape is `docs/spec/interfaces/card.schema.json`; cards wait in `MUSE_HOME/cards.sqlite`.

The existing read-only dashboard server stays as it is for `muse dash`. `python -m muse.home --fake` serves the same API from canned data for app development.

### 4.2 Router endpoint (exists)

OpenAI-compatible at `http://127.0.0.1:4000/v1`, described in the README. Other apps on the home use it with their own app token.

### 4.3 Tool calls

MCP between the helper and each tool process. The mandate token travels as request metadata and is verified by the sandbox before the tool starts.

### 4.4 Core API (inside the app)

Rust functions exposed to the shell through Tauri commands: the Vault, Mandates, Receipts, Sync and Link interfaces listed in section 2. The same crate builds for desktop, iOS and Android.

---

## 5. Data and where it lives

| Data | Where | Encrypted at rest | Synced |
|---|---|---|---|
| Provider keys | Home keystore or `.env` 600; copy in vault | Yes | Vault copy only |
| Usage ledger | Home, SQLite | No (holds no content) | Summary only |
| Queue and jobs | Home, SQLite | Prompts erased when a job ends | Job list |
| Catalogue | Shipped with the app; overrides in sync | No | Overrides |
| Mandates, revocations | Vault | Yes | Yes |
| Receipts | Vault; home holds its own chain | Yes | Yes |
| Helper memory | Vault | Yes | Yes |
| Health records (add-on) | Vault | Yes | Only between the person's own devices |
| Wallet keys | Device secure hardware or the wallet provider | Yes | Never |

Nothing is stored on a Muse server in v1. The only shared infrastructure is the iroh relays (ciphertext), a stateless push relay that forwards an empty wake signal to phones and never sees content, and whatever the person connects themselves.

---

## 6. Security model

| Threat | Defence |
|---|---|
| A malicious or buggy skill | Sandbox per call; no keys; token names what it may touch; installed by hash |
| Text in a web page or file telling the helper what to do | Tool output is data, never instructions; writes need a confirm card; the token limits what any instruction could reach |
| A compromised dependency release | Pinned by digest and hash; `scripts/verify_litellm.py`; keys only in the router process |
| Theft of a device | Vault needs biometrics or PIN; revoke and rotate from another device |
| The VM's cloud account is taken over | The VM holds only what its mandate allows; private records are ciphertext there; revoke its mandate and rebuild |
| The company disappears | No server to lose; open formats; export at any time |
| The person loses everything | Recovery key; said plainly at first run |
| Overspending | Daily paid cap in the router; spending limit on chain; confirm threshold |

Not defended in v1, and stated as such: an attacker with full control of an unlocked device; a provider that ignores its own stated terms.

---

## 7. What is deliberately left out of v1

- A public skill marketplace.
- Sending mail, posting, or any write to a third party without a card.
- Mainnet, any token, redemption of credits for money.
- Real health data.
- Phones running chores unattended.
- A Rust router.
- Any Muse-run server that stores or sees a person's data. (The stateless push relay for phones is the one service Muse runs.)
