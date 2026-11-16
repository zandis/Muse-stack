# Add an always-on helper (Oracle Cloud, free allowance)

> **Not tested yet.** Everything in this folder was written and tested without access to an Oracle account. Nothing has been deployed to a real cloud account. The tests check the files, the scripts and the logic on an ordinary computer; they cannot show that Oracle accepts the stack or that the helper boots. The list of what must be tried on a real account first is at the end, under [Not tested yet](#not-tested-yet).

## What you get

A small computer in your own Oracle Cloud account that stays on. It runs the parts of Wardmuse that should keep going when your laptop and phone are off: the router that shares your AI allowances, the queue of background jobs, and your scheduled chores.

- It is yours. It lives in your Oracle account, and Wardmuse runs no server that sees your data.
- Nothing on it can be reached from the internet. It only makes outgoing connections.
- It keeps itself up to date and makes an encrypted backup every night.

## What it costs

Nothing, as long as you stay inside Oracle's Always Free allowance. This stack is sized to fit it exactly:

| | This helper uses | Oracle's free allowance |
|---|---|---|
| Processors | 2 OCPU | 2 OCPU (1,500 OCPU hours a month) |
| Memory | 12 GB | 12 GB (9,000 GB hours a month) |
| Disk | 50 GB | 200 GB in total |
| Backup storage | a few MB | 20 GB |

Source: Oracle, "Always Free Resources", <https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm>, read on 13 November 2026.

Three things to know:

- The allowance is for your whole account. If you already run another free ARM instance or large disks, the totals add up, and anything above the allowance is not free.
- Oracle may ask for a payment card when you open the account, to check who you are.
- Oracle may take back a free instance that looks unused. The helper guards against that; see [Idle guard](#idle-guard).

## The three steps

1. **Create it.** Press the button. Oracle's page opens with everything filled in.
2. **Approve it there.** Choose where it goes (three lists, any choice works), tick "Run apply", press Create. Wait about ten minutes.
3. **Pair it.** In the Wardmuse app: Devices, "Add an always-on helper", step 3. *This step is not built yet* (it arrives with the device link, work packages D1 to D3). Until then the helper runs but only an early tester with the SSH option can give it keys and chores; see [Reaching the helper](#reaching-the-helper).

The button, for the page or app that offers it:

```markdown
[![Deploy to Oracle Cloud](https://oci-resourcemanager-plugin.plugins.oci.oraclecloud.com/latest/deploy-to-oracle-cloud.svg)](https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=<RELEASE-ZIP-URL>)
```

`<RELEASE-ZIP-URL>` is the address of the stack zip that the owner publishes with a release (see [Publishing](#publishing-for-the-owner)). It is a placeholder on purpose: no such address exists yet.

## What Oracle will ask for

- **To sign in**, or to open an account (name, email, a phone number, possibly a card).
- **Region.** Choose your home region, the one you picked when you opened the account. The free allowance for compute only applies there.
- **Compartment.** A folder in your account. Keep the one already chosen.
- **Availability domain.** Any. If Oracle answers "out of host capacity", free ARM machines are sold out there for the moment: go back, pick another one and apply again, or try later.
- **Terms of use for the stack**, and a final "Create".

Everything else has a default and sits under "Advanced".

## How to delete everything

1. In the Oracle Console open **Developer Services, Resource Manager, Stacks**, and open the stack.
2. If backups were on: open **Storage, Buckets**, open the bucket named `muse-backups-...`, select all objects and delete them. Oracle does not delete a bucket that still holds files.
3. Back in the stack press **Destroy**. This removes the helper, its disk, its network, the bucket and the two identity entries.
4. Delete the stack itself.

After that nothing of the helper is left in the account, and nothing is billed. Your backups are gone with the bucket; a copy on your own devices is the only one that remains.

---

# Technical section

## Every resource the stack creates

| Resource | Name | Why |
|---|---|---|
| `oci_core_vcn` | `muse-helper` (10.77.0.0/24) | A network of its own, so nothing existing is touched |
| `oci_core_internet_gateway` | `muse-helper-outbound` | Outgoing traffic only has a path out through this |
| `oci_core_route_table` | `muse-helper` | Default route to the gateway |
| `oci_core_security_list` | `muse-helper-no-inbound` | Outbound TCP 443 and 80; **no inbound rule** |
| `oci_core_default_security_list` | `muse-helper-default-emptied` | Takes over the default list every VCN is born with (it allows SSH in) and leaves it with no rules |
| `oci_core_subnet` | `muse-helper` (10.77.0.0/28) | Uses only the no-inbound list |
| `oci_core_instance` | `muse-helper` | `VM.Standard.A1.Flex`, 2 OCPU, 12 GB, Ubuntu 24.04 aarch64, 50 GB disk |
| `oci_objectstorage_bucket` | `muse-backups-<suffix>` | Private bucket for encrypted backups (only with backups and identity on) |
| `oci_objectstorage_object_lifecycle_policy` | on that bucket | Deletes backups older than 35 days; clears unfinished uploads |
| `oci_identity_dynamic_group` | `muse-helper-<suffix>` | Its only member is this one instance |
| `oci_identity_policy` | `muse-helper-backups-<suffix>` | That group may read the backup bucket and manage its objects; Oracle's storage service may run the lifecycle rule |

Files: `versions.tf` (provider pinned to exactly `oracle/oci 9.12.1`), `variables.tf`, `network.tf`, `compute.tf`, `storage.tf`, `iam.tf`, `outputs.tf`, `release_pin.tf`, `schema.yaml` (the form), `cloud-init.yaml.tftpl` (first boot). Outputs are the instance's identifier, its name, size, where backups go and what can reach it; no output and no variable is a secret.

## The network: nothing in

**Chosen: a public subnet whose security list has no inbound rule.** The instance has a public address because an internet gateway carries outbound traffic only for instances that have one. No rule lets anything start a connection to it, and the rules are stateful, so replies to connections the helper starts come back by themselves.

**Not chosen: a private subnet behind a NAT gateway.** It has the same effect and no public address, but Oracle's Always Free page does not list NAT gateways and the NAT gateway page states no price, so I could not confirm it is free.

Three layers keep the helper unreachable:

1. The cloud network: no inbound rule (above).
2. The services: the router binds `127.0.0.1:4000` (pinned in its unit, whatever a settings file says), the home control API is a Unix socket, nothing else listens. First boot fails and stops everything if any TCP socket listens beyond loopback.
3. With no tester key, `sshd` is stopped, disabled and masked.

Outbound is TCP 443 (AI providers, the Wardmuse release, Oracle's own services) and TCP 80 (Ubuntu's package mirrors; apt checks signatures). The metadata service, DNS and time are on `169.254.0.0/16`, where Oracle does not enforce security rules. UDP is closed; the device link (D1) will need it and must add its own rule then.

Because no ICMP can come in, the helper sets `net.ipv4.tcp_mtu_probing=1` so TCP finds the path MTU by itself.

**The one exception, off by default.** For early testers the form has "SSH public key" and "SSH allowed from". Only when **both** are filled does the stack add one inbound rule, TCP 22 from that one address (a /24 or smaller; "anywhere" is refused), and pass the key to the instance.

## What runs

Native Python, no Docker. On 2 OCPU and 12 GB a container runtime would cost a root daemon, a few hundred MB of memory and over a GB of image for one process. The install is `pip install --require-hashes -r requirements/proxy.lock` (pip refuses any file whose SHA-256 is not in the repository), followed by `scripts/verify_litellm.py`; systemd provides the isolation a container would. The trade-off: this depends on every locked package having an aarch64 wheel, which has not been tried.

| Unit | What | Runs as | Network |
|---|---|---|---|
| `muse-proxy.service` | The router (LiteLLM) on `127.0.0.1:4000`. The only process that holds provider keys | `muse` | internet |
| `muse-home.service` | `muse home serve`: the control API on `MUSE_HOME/home.sock` | `muse` | loopback only |
| `muse-queue.service` | `muse queue run` | `muse` | loopback only |
| `muse-chores.timer` | `muse chores tick`, every minute | `muse` | loopback only |
| `muse-catalog-check.timer` | `muse catalog check --pages --json`, Sundays 04:20; result in `/var/lib/muse/vm/catalog-check.json` | `muse` | internet |
| `muse-backup.timer` | Backup, daily 02:10 | `muse` | internet |
| `muse-update.timer` | Wardmuse update check, nightly 03:00 (off until configured) | root | internet |
| `muse-vm-monitor.service` | Samples for the status card; the idle guard | `muse` | none |

Every service unit has `NoNewPrivileges`, `ProtectSystem=strict`, `ProtectHome`, `PrivateTmp`, `PrivateDevices`, kernel and control-group protection, `RestrictAddressFamilies`, a memory ceiling and a CPU quota. Units that need no internet have `IPAddressDeny=any` with `IPAddressAllow=localhost`. The services can write to `/var/lib/muse` and nowhere else; the code in `/opt/muse` is owned by root and read-only to them. The `muse` account has no password, no login shell and no sudo.

Layout on the VM: code in `/opt/muse/releases/<version>` with `/opt/muse/current` pointing at the running one; state in `/var/lib/muse` (`home/` is `MUSE_HOME`, `vm/` holds samples and job results, `muse.env` holds the keys, mode 0600); settings without secrets in `/etc/muse/vm.conf`; how first boot ended in `/var/lib/muse-setup/DONE` or `FAILED`.

### First boot

The instance's user data (cloud-init) is small on purpose: 3.9 KB rendered, 2.4 KB after gzip and base64 as sent, against the commonly quoted cloud-init limit of 16 KB and Oracle's 32,000 bytes for all instance metadata. It holds no secret (user data is readable by every process on the VM and stored in the stack's state). It does three things:

1. Writes `/etc/muse/vm.conf`.
2. Downloads one fixed Wardmuse release over https and compares its SHA-256 with the one baked into the stack. A mismatch, a missing checksum, a plain-http address, or a version that is a branch or a moving name (`main`, `latest`, ...) stops it there. It never clones a branch.
3. Runs `deploy/vm/bootstrap.sh` from that verified release: packages from Ubuntu's archive (`python3-venv`, `age`, `curl`), the `muse` account, automatic security updates with a reboot window at 03:30, the hash-locked install and its check, the master key, the backup key, the units, and a health check.

The proxy master key is generated on the VM, written straight into `/var/lib/muse/muse.env` (0600) and never printed: no script uses `set -x`, and the tests search every script for a secret reaching `echo`, a log or a command line. Each step runs once and is skipped on a second run. If a step fails, `/var/lib/muse-setup/FAILED` names it, every Wardmuse unit is stopped and disabled, and nothing is left half-configured and listening.

### Sizing on 2 OCPU and 12 GB

These are estimates, not measurements; measuring them is on the list at the end.

| | Expected memory | Ceiling | Expected CPU |
|---|---|---|---|
| Ubuntu, its agents | 0.4 to 0.6 GB | | about 1% |
| Router (1 worker) | 0.5 to 0.9 GB | 3 GB | bursts while a request is routed |
| Home API | under 0.1 GB | 512 MB | idle |
| Queue | about 0.1 GB | 1 GB | idle between jobs |
| Chores pass | 0.1 to 0.2 GB for a few seconds each minute | 1 GB | under 1% on average |
| Monitor and idle guard | about 15 MB | 128 MB | about 2.5% on average when the guard is on; at most one core, lowest priority |
| Nightly jobs | backup under 0.2 GB; an update's install up to about 1 GB | 1 GB, 2 GB | a few minutes |

Real use at rest is therefore about 1.2 to 1.8 GB, 10% to 15% of memory, with almost no processor or network, which is why the idle guard exists.

Deliberately **not** run: local chat models (at 2 OCPU they manage a few tokens a second at best, which is too slow to be useful; estimate from `docs/ecosystem-review-2026-11.md` §3.3), Docker, Postgres spend logs, the dashboard, a reverse proxy or TLS endpoint, the LINE webhook tunnel (D6), sync and the device link (D1 to D3).

## Updates

- **Operating system.** `unattended-upgrades` installs Ubuntu security updates daily and reboots at 03:30 when one requires it.
- **Wardmuse.** `muse-update.sh`, nightly at 03:00, **off by default**. It needs two settings that only the owner can provide: `MUSE_UPDATE_MANIFEST_URL` (https address of a release manifest) and `MUSE_UPDATE_PUBKEY` (the Ed25519 public key the manifest is signed with). No address or key is invented here. With both set it: fetches the manifest and its `.sig`; checks the signature; refuses a moving version, a non-https address, a malformed checksum or a downgrade; downloads the release and checks its SHA-256; unpacks it next to the running one; builds a new environment there from the lock file with `--require-hashes`; runs `verify_litellm.py`, starts the CLI and runs `scripts/validate_config.py`; switches the `current` link; restarts; waits for the router. If the router does not come up it switches back, restarts the previous release and records the failure. The running release and the one before are kept.

  Manifest: `{"channel": "stable", "version": "0.3.0", "url": "https://.../muse-0.3.0.tar.gz", "sha256": "<64 hex>"}`; the signature file is the base64 of an Ed25519 signature over the manifest's exact bytes. The private key and where it is kept are the owner's decisions.

## Backups and what they contain

Daily at 02:10, `muse-backup.sh`:

1. **Copies `MUSE_HOME` without stopping anything.** SQLite databases (ledger, queue, cards, records, chores) are copied with SQLite's online backup API, so each copy is consistent, and checked with `PRAGMA integrity_check`.
2. **Encrypts the copy with `age` before it is kept anywhere.** Nothing unencrypted is ever uploaded; without `age` or a valid recipient the job stops.
3. Keeps the newest 3 on the VM and uploads the file and its SHA-256 to the bucket under `backups/`, keeping the newest 14 there.

**In a backup:** every regular file under `MUSE_HOME`. That includes the databases, the apps file, hashed device tokens, queued prompts and results, and the device signing key (`device.key`).

**Not in a backup:** the private half of the backup key itself; logs, pid files, sockets, the rendered router config, SQLite side files. **Provider keys and the master key are not in it either**: they live outside `MUSE_HOME` in `/var/lib/muse/muse.env`. Setting `MUSE_BACKUP_INCLUDE_KEYS=1` puts that file inside the same encrypted archive.

**Who can read a backup.** Whoever holds the private key it was encrypted to:

- With `MUSE_BACKUP_RECIPIENT` set (the public key a paired device will supply; pairing is not built yet): only that device.
- **Until then: a key generated on the VM**, whose private half stays in `MUSE_HOME/backup-key/identity.txt`. This protects the backup if the bucket is exposed. **It does not protect you if the VM is lost**: the only key that opens the backup is lost with it, unless a tester copied that file off the VM. The helper's status says this in those words.

**Uploading without a stored key.** The VM signs requests with the certificate Oracle places in its metadata service and rotates several times a day (an "instance principal"); the dynamic group and policy in `iam.tf` limit that identity to this one bucket. The uploader is `deploy/vm/oci_object.py`, under 300 lines using only `cryptography`, which `requirements/proxy.lock` already pins by hash. Oracle's command line tool was not used because it is a large package with its own dependency tree that would need a second lock file. The script follows the steps Oracle's Python SDK takes and is tested against a local stand-in that verifies every signature; it has never talked to Oracle.

**If the identity entries cannot be created.** Dynamic groups and policies live at tenancy level. The person who opened a free account is an administrator and can create them; Oracle's pages do not say they are unavailable in a free tenancy, and do not say so explicitly that they are available either. Someone with rights in one compartment only will see Apply fail with a permissions message: switch off "Let the helper save backups to its own private bucket". No bucket is then created, backups stay on the VM's own disk, and the status says plainly that a lost VM means a lost backup.

## Restore

`muse-restore.sh` rebuilds `MUSE_HOME` on a fresh helper:

```bash
sudo /opt/muse/current/deploy/vm/muse-restore.sh --identity /path/to/identity.txt        # check only
sudo /opt/muse/current/deploy/vm/muse-restore.sh --identity /path/to/identity.txt --yes  # put it in place
```

It fetches the latest backup from the bucket, checks its SHA-256, decrypts it, checks every file against the manifest inside and every database with SQLite's integrity check, stops the services, moves the existing `MUSE_HOME` aside (never deletes it), puts the restored one in place and starts the services. Any failure before the swap leaves everything as it was. `--with-keys` also restores provider keys when the backup holds them.

"Rebuild from backup" as one tap in the app needs the key to reach the new VM through the pairing channel, which does not exist yet.

## Idle guard

**Oracle's rule**, quoted from the Always Free page (re-read on 15 November 2026): "Idle Always Free compute instances may be reclaimed by Oracle. Oracle will deem virtual machine and bare metal compute instances as idle if, during a 7-day period, the following are true: CPU utilization for the 95th percentile is less than 20%; Network utilization is less than 20%; Memory utilization is less than 20% (applies to A1 shapes only)." All three must be true, so staying above one is enough.

**On by default for an Always Free account, off for a paid one.** The form asks which kind of Oracle account you have (`account_type`: `always_free`, the default, or `paid`), and the guard's setting (`idle_guard`) defaults to `auto`, which follows it. Set `idle_guard` to `on` or `off` to override. On the VM the result is in `/etc/muse/vm.conf` as `MUSE_ACCOUNT_TYPE` and `MUSE_IDLE_GUARD` (`on`, `off` or `auto`); change it there and run `sudo systemctl restart muse-vm-monitor`. Oracle's page speaks of "Always Free compute instances" and does not say whether the rule still applies once an account is upgraded; off for paid accounts is this project's choice, not something the page states.

**What the helper does.** A router, a queue and a once-a-minute chores pass use almost no processor or network and 10% to 15% of 12 GB, so an honest, working helper would still be measured as idle. `muse-vm-monitor` uses the processor, because that part of the rule applies to every shape and a 95th percentile only needs just over 5% of the minutes to be busy:

- It watches each wall-clock minute. When no minute in the last ten reached 25% by itself, it runs the processor at about 25% of the machine (half a core on 2 OCPU) for one minute and a few seconds past it. That is one minute in ten, twice what the 95th percentile needs, and adds about 2.5% to the week's average processor use.
- Real work comes first. Any minute that reaches 25% on its own restarts the count, so a busy helper never triggers it. During its minute it measures everything else once a second and stops at once when other work uses more than 10% of the processor. It runs at the lowest priority (`Nice=19`, `CPUWeight=1`) and can never use more than one core.
- It never holds memory and never sends traffic: the unit has a 128 MB memory ceiling and no network at all.
- Once a day it writes one plain line to the journal (`journalctl -u muse-vm-monitor`) saying whether it was on and why, how many minutes it ran, how many minutes real work reached 25% by itself, and how many times it backed off.

**Said plainly:** this is deliberate processor use to stay above a threshold, not useful work. It is the cheapest honest way we found: a few watts on a machine that is otherwise free, with nothing hidden. Real work that keeps the machine busy (for example local embeddings, once they run on the VM) replaces it automatically, because those minutes count.

**What Oracle says about it.** The Always Free page states the rule and nothing more: it does not say whether keeping an instance above the thresholds on purpose is allowed or forbidden. I did not review Oracle's full service terms for this, so I could not confirm either way. The rule's evident purpose is to take back capacity nobody uses; a helper that runs your chores every minute is in use, though lightly. If you would rather not do this, turn it off: "Keep the helper from being reclaimed as idle" set to `off` in the form, or `MUSE_IDLE_GUARD=off` in `/etc/muse/vm.conf`. Oracle may then reclaim the instance after 7 quiet days; the backup is what brings it back.

**The metric and how to verify it.** The target is the instance's **CpuUtilization** in the `oci_computeagent` metric namespace. In the Oracle Console: Compute, Instances, `muse-helper`, Metrics, "CPU Utilization"; on a quiet helper it should show a short step to about 25% every ten minutes. On the VM, `sudo muse-vm-status --pretty` shows the last 7 days, the guard's state and `oracle_idle_rule.would_count_as_idle`. That Oracle's agent measures in one-minute buckets on the wall clock is an assumption to check against the Console graph; the few extra seconds of each guard minute are there in case the two clocks differ slightly.

## The status card

`muse-vm-status` prints JSON for the always-on helper's card in Devices: running since, the last 7 days of CPU, memory and network (mean and 95th percentile from one sample a minute it collects itself), whether Oracle's idle rule would apply, the idle guard's state, the last backup (time, size, where it went, who can read it), the last update check, the catalogue check, whether a reboot is pending, and the free-allowance estimate: OCPU hours and GB hours used this month against 1,500 and 9,000, with a projection to the end of the month (a 31-day month at 2 OCPU and 12 GB is 1,488 and 8,928). The estimate comes from the VM's own samples; Oracle's Cost Analysis is the authority. The network percentage assumes 1 Gbps per OCPU, because Oracle's page does not say what it divides by.

## Reaching the helper

With the defaults there is no way in, by design: the app will reach it over the outbound-only device link once pairing exists (D1 to D3). **Until then** a helper deployed with the defaults runs, updates and backs itself up, but cannot be given provider keys or chores.

Early testers fill in both SSH fields in the form. Then:

```bash
ssh ubuntu@<tester_ssh_address from the stack's outputs>
cat /var/lib/muse-setup/DONE              # or FAILED, which names the step
sudo muse keys set GEMINI_API_KEY         # prompts; never on the command line
sudo systemctl restart muse-proxy
sudo muse status
sudo muse-vm-status --pretty
```

To use the router from your own computer, tunnel to it: `ssh -N -L 4000:127.0.0.1:4000 ubuntu@<address>`. Keep a copy of `/var/lib/muse/home/backup-key/identity.txt` somewhere safe if you want to be able to restore after losing the VM.

## Check that nothing listens publicly

On the VM (testers):

```bash
sudo ss -tlnH
```

Expected: `127.0.0.1:4000` (the router) and the system resolver on `127.0.0.53:53` and `127.0.0.54:53`. With the tester option, also `:22`. Anything else is a fault. `sudo ss -ulnH` should show only loopback addresses and the DHCP client on port 68.

In Oracle: Networking, Virtual cloud networks, `muse-helper`, Security Lists. Both lists must show **0 ingress rules** (one rule for TCP 22 from your address with the tester option). With Oracle's command line tool:

```bash
oci network security-list list --compartment-id <compartment OCID> --vcn-id <VCN OCID> \
  --query 'data[].{name:"display-name", ingress:"ingress-security-rules"}'
```

Both entries should show `"ingress": []`.

## Limits

- One helper per compartment with the default names; set `name_suffix` for a second.
- Settings are applied at first boot only. Changing a value in the stack later does not reconfigure a running helper (the stack ignores such changes rather than rebuild the instance and its disk).
- The image is "the newest Ubuntu 24.04 for this shape" at creation time; `image_ocid` pins a specific one.
- `terraform destroy` fails while the bucket holds objects (see "How to delete everything").
- Backups larger than 512 MB are kept locally and not uploaded (`MUSE_BACKUP_MAX_MB`).
- A restore downloads the backup into memory first; fine for the sizes expected here.

## Publishing (for the owner)

The repository is private, and neither Oracle's stack page nor a new VM can sign in to it. Both files below must be at https addresses that anyone can fetch.

```bash
# 1. The release tarball: one commit, reproducible, with its checksum.
deploy/vm/build-release.sh --version 0.2.0 --ref v0.2.0 --out dist/

# 2. The stack zip, pinned to that tarball. Same inputs, same bytes; the SHA-256 is printed.
deploy/oracle-stack/build-zip.sh --version 0.2.0 \
    --release-url https://<where you published it>/muse-0.2.0.tar.gz \
    --release-sha256 <from step 1> --out dist/muse-oracle-stack-0.2.0.zip
```

Publish both as release assets and put the zip's address in the button in place of `<RELEASE-ZIP-URL>`. Oracle's page lists as supported sources GitHub and GitLab archive addresses and an Object Storage pre-authenticated address; whether a release-asset address is accepted has to be tried.

## Facts checked, with sources

Read on 13 November 2026.

| Fact | Source |
|---|---|
| A1 Flex is free for the first 1,500 OCPU hours and 9,000 GB hours a month, "equivalent to 2 OCPUs and 12 GB of memory" | [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) |
| The idle rule quoted above (re-read on 15 November 2026); nothing on that page about keeping an instance busy, or about upgraded accounts | same |
| 200 GB of block and boot volume in total, 5 volume backups; a new instance gets a 50 GB boot volume. The page gives the minimum boot volume as 47 GB in one place and 50 GB in another | same |
| 20 GB of Object Storage and 50,000 API requests a month | same |
| Ubuntu is an Always Free image for A1; free compute and free block volumes must be in the home region; free tenancies get up to 2 VCNs and 10 TB of outbound data a month; "out of host capacity" means try another availability domain or later | same |
| Button: `https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=<package-url>`; the image address; sources are GitHub, GitLab or an Object Storage pre-authenticated address | [Using the Deploy to Oracle Cloud Button](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Tasks/deploybutton.htm) |
| The zip's root is the working directory and must hold at least one `.tf` file, no `.terraform` folder and no state file; `region` is the only provider field needed; `tenancy_ocid`, `compartment_ocid`, `region` and `current_user_ocid` are filled in when declared | [Terraform Configurations for Resource Manager](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager.htm) |
| `schema.yaml` sits in the root; needs `schemaVersion` and `variables`; the picker types used here | [Extend Console Pages Using Schema Documents](https://docs.oracle.com/en-us/iaas/Content/ResourceManager/Concepts/terraformconfigresourcemanager_topic-schema.htm) |
| A subnet uses the default security list unless a custom one is given; "Without security rules, no traffic is allowed in and out"; stateful rules let responses back; rules are not enforced for `169.254.0.0/16` | [Security Rules](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securityrules.htm) |
| The default security list allows SSH in (TCP 22), ICMP type 3 code 4, and all egress | [Security Lists](https://docs.oracle.com/en-us/iaas/Content/Network/Concepts/securitylists.htm) |
| A NAT gateway gives outbound-only access; the page states no price | [NAT Gateway](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/NATgateway.htm) |
| Instance principals: certificates created, assigned and rotated automatically ("multiple times each day"); a dynamic group plus a policy; dynamic groups are created at tenancy level; `Allow dynamic-group <name> to <verb> <resource-type> in compartment <name>` | [Calling Services from an Instance](https://docs.oracle.com/en-us/iaas/Content/Identity/Tasks/callingservicesfrominstances.htm) |
| Matching rules can use `instance.id` and `instance.compartment.id` | [Writing Matching Rules](https://docs.oracle.com/en-us/iaas/Content/Identity/dynamicgroups/Writing_Matching_Rules_to_Define_Dynamic_Groups.htm) |
| Lifecycle rules run only after a policy in the root compartment allows `service objectstorage-<region>` to manage objects; rules count in days and can filter by prefix | [Using Object Lifecycle Management](https://docs.oracle.com/en-us/iaas/Content/Object/Tasks/usinglifecyclepolicies.htm) |
| Metadata v2 endpoints need `Authorization: Bearer Oracle`; the v1 endpoints can be switched off | [Getting Instance Metadata](https://docs.oracle.com/en-us/iaas/Content/Compute/Tasks/gettingmetadata.htm) |
| Provider arguments used (`shape_config`, `source_details`, `create_vnic_details`, `instance_options`, `agent_config`, security list rules, images lookup, lifecycle policy); `user_data` is base64; instance metadata is at most 32,000 bytes | the provider's own pages, [github.com/oracle/terraform-provider-oci](https://github.com/oracle/terraform-provider-oci/tree/master/website/docs) |
| Provider `9.12.1` was released on 5 November 2026 | [provider releases](https://github.com/oracle/terraform-provider-oci/releases) |
| How an instance obtains a token: `POST /v1/x509` signed with keyId `<tenancy>/fed-x509/<SHA-1 fingerprint>`, then requests signed with keyId `ST$<token>` | [oci-python-sdk](https://github.com/oracle/oci-python-sdk/tree/master/src/oci/auth) source |

### What I could not confirm

- Whether dynamic groups and policies work in a free tenancy, and which exact permission creating a dynamic group needs. The pages do not restrict them and do not say.
- Whether a NAT gateway is free (hence the public-subnet design).
- That a security list may be saved with zero rules, and that `oci_core_default_security_list` with no rule blocks empties the default list. Both follow from the pages but are not stated.
- Whether stateful rules also let path-MTU ICMP messages back in (hence TCP MTU probing).
- How Oracle computes memory and network utilization, the length of its CPU metric's buckets, and whether the idle rule applies to an upgraded (pay-as-you-go) account.
- Whether Oracle's terms address keeping an instance above the idle thresholds on purpose.
- Which Terraform versions Resource Manager runs, and whether it can fetch provider `9.12.1`.
- Whether the button accepts a release-asset address.
- Details of Oracle's Ubuntu image taken from memory, not from a page: that cloud-init works with the v1 metadata endpoints off and accepts gzip user data, that `users: []` leaves no default account, that package mirrors use plain http, that time comes from `169.254.169.123`, the image filter values `Canonical Ubuntu` and `24.04`, and that an ephemeral public address is free.

## Not tested yet

Nothing was deployed. `terraform` was not available where this was written, so the `.tf` files were never run through `terraform validate` or a plan; the tests use a small strict reader for the HCL this stack uses. Before any tester sees this, on a real free tenancy:

1. `terraform validate` and a plan; then Apply from Resource Manager using the zip. Does the provider pin resolve? Does the form render and show only three required choices?
2. Does the button accept the published zip address?
3. Does the instance get created with no SSH key, IMDS v1 off and gzip user data, and does first boot reach `/var/lib/muse-setup/DONE`? How long does it take (target: under 10 minutes)?
4. Does every package in `requirements/proxy.lock` have an aarch64 wheel, so the hash-locked install completes on the VM?
5. With the security lists as created: do apt, the release download and provider calls work; is anything reachable from outside (scan the public address); are both lists at 0 ingress rules?
6. Do the dynamic group, the policy and the lifecycle rule get created by a free account's administrator, in order, and does the first upload from `oci_object.py` succeed? Then a restore on a second VM.
7. Do the hardened units start under systemd (LiteLLM under `ProtectSystem=strict`, the `IPAddressDeny` units reaching `127.0.0.1:4000`)? Does anything else on Oracle's image listen on TCP and trip the health check?
8. Real memory and CPU of each service, to replace the estimates above; the idle guard's effect on the Console's CPU Utilization graph over 7 days (a step to about 25% every ten minutes on a quiet helper).
9. An unattended-upgrades reboot; a Wardmuse update and a forced rollback, once a manifest and key exist.
10. Destroy: does it remove everything once the bucket is empty?
