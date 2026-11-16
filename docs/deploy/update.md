# Updating the VM

The production VM is one Oracle Cloud Always Free ARM machine. The proxy runs from Docker. Secrets live only in the env file on that machine. The git repository is public, so nothing in GitHub Actions is allowed to log in to the VM or to read that file.

The VM updates itself. Once a night, and whenever you start it by hand, it downloads a signed release, checks the signature, and switches only if a short smoke test passes. If the test fails, it puts the previous release back and restores the data it snapshotted first.

You do four things by hand before the first automatic update. Nothing in the repository can do them for you:

1. Generate the signing key, and keep the private half off the VM and off GitHub.
2. Put the public half in `/etc/muse/vm.conf` on the VM.
3. Create the healthchecks.io check and put its URL in a file on the VM.
4. Run the one-time migration from the git clone.

## What is on the VM after migration

| Path | What it is |
| --- | --- |
| `/opt/muse/releases/<version>` | One unpacked release. The running one and the one before it are kept. |
| `/opt/muse/current` | Symlink to the running release. |
| `/etc/muse/muse.env` | Copy of the clone's `.env`. Mode 0600. The updater never writes it. |
| `/etc/muse/vm.conf` | Settings that are not secrets: manifest URL, public key, `MUSE_RUNTIME=docker`. |
| `/etc/muse/healthchecks.url` | Optional. One https URL. Mode 0600. The repository only names this file. |
| `/etc/muse/heartbeat.url` | Optional second URL, pinged every five minutes. |
| `/var/lib/muse/home` | Ledger and other state (`MUSE_HOME`). |
| `/opt/muse-stack` | The old clone, left in place so you can go back. Its `.env` is not modified. |

Logs from an update: `journalctl -u muse-update`. The last result is `/var/lib/muse/vm/update/last.json`.

## 1. Generate the signing key

On your own machine, not on the VM:

```bash
python3 -m pip install "cryptography>=42"
python3 scripts/sign_manifest.py generate --dir ~/.config/muse
```

That writes `~/.config/muse/release.key` (mode 0600) and prints one line, the public key. The private key is not printed. Back the private key up somewhere that is not the VM and not a GitHub secret, unless you deliberately choose the fallback in the next section.

Copy only the public line. You will paste it into `/etc/muse/vm.conf` as `MUSE_UPDATE_PUBKEY`.

## 2. Why signing stays on your machine

The check on the VM is what stops a bad release. That check is worthless if the private key lives in the same place an attacker would change the workflow. GitHub Actions therefore builds the tarball and stops. The release is a **draft**. Drafts are not served from `/releases/latest`, so the VM cannot see an unsigned manifest.

Fallback, if you want Actions to sign after you click Approve: create a GitHub Environment named `release`, add yourself as a **required reviewer**, and only then store the private key there. The order matters. GitHub creates an environment with no reviewer the first time a job names one, and a job could sign before you noticed.

1. Environment `release`, required reviewer: you. Read the diff of `.github/workflows/release.yml` and `scripts/sign_manifest.py` before every approval. A change there can print the secret.
2. Environment secret `MUSE_RELEASE_SIGNING_KEY`: the one line inside `release.key` (base64 of the 32-byte seed).
3. Repository variable `MUSE_SIGN_IN_CI` set to `true`.

Leave the variable unset and the sign job never runs. That is the default.

## 3. Publish a release

Push a tag `v0.2.0` (three numbers; not `latest` or `main`), or run the `release` workflow by hand with that version. The workflow runs on GitHub-hosted runners only. It does not run on pull requests.

It uploads `muse-0.2.0.tar.gz` and `stable.json` to a draft release. On your machine, check the checksum against a local build of the same commit, then sign the manifest **bytes you downloaded**. Do not regenerate the manifest: the expiry timestamp would change and the signature would not match.

```bash
git fetch origin tag v0.2.0
git checkout v0.2.0
bash deploy/vm/build-release.sh --version 0.2.0 --ref v0.2.0 --out /tmp/muse-check
gh release download v0.2.0 --pattern 'muse-*' --pattern stable.json --dir /tmp/muse-rel
# The sha256 printed by the local build must equal the "sha256" field inside stable.json.
python3 -c 'import json; print(json.load(open("/tmp/muse-rel/stable.json"))["sha256"])'
python3 scripts/sign_manifest.py sign \
  --manifest /tmp/muse-rel/stable.json \
  --key ~/.config/muse/release.key \
  --out /tmp/muse-rel/stable.json.sig
gh release upload v0.2.0 /tmp/muse-rel/stable.json.sig
gh release edit v0.2.0 --draft=false
```

The manifest is valid for 30 days. After that the VM refuses it, including when the version has not changed, and the healthchecks ping fails. Run the workflow again and sign the new `stable.json` to refresh the date. Rebuilding the same commit produces the same tarball bytes.

The VM fetches:

`https://github.com/zandis/Muse-stack/releases/latest/download/stable.json`

and the same address with `.sig` added. The tarball URL is inside the signed file.

## 4. Migrate the current clone

Do this on the VM, in a window when you can watch it. The proxy is recreated at the end; until that `up`, the clone keeps serving. Take a copy of the env file and the data directory first if you want a second copy. The script does not edit either original.

Run the script from a **second checkout** of the branch that contains it. Do not `git pull` inside `/opt/muse-stack`. That clone must stay clean: the script packs its committed files, and it refuses to run if the clone has uncommitted changes. The packed tree is the commit the clone is on now (6aefbb8 when this was written). Moving to a newer commit happens later, when you publish a signed release and the updater installs it.

```bash
sudo apt-get update
sudo apt-get install -y python3-cryptography ca-certificates curl git
# After ccr-645e0264-bn082z has become main, clone main instead of this branch.
sudo git clone --branch ccr-645e0264-bn082z --depth 1 \
  https://github.com/zandis/Muse-stack.git /tmp/muse-tools
# confirm the live clone, then leave it alone
git -C /opt/muse-stack rev-parse HEAD
git -C /opt/muse-stack status --porcelain

sudo bash /tmp/muse-tools/deploy/vm/migrate-from-clone.sh --clone /opt/muse-stack --dry-run --home /root/.muse
sudo bash /tmp/muse-tools/deploy/vm/migrate-from-clone.sh --clone /opt/muse-stack --yes --home /root/.muse
```

`--home` is the directory compose has been using for `MUSE_HOME`. On this VM that has been root's `~/.muse`. The script copies it with SQLite's backup and leaves the source in place.

`--dry-run` prints the plan and writes nothing. `--yes` then:

- copies `/opt/muse-stack/.env` to `/etc/muse/muse.env` (mode 0600) and checks the original still matches;
- unpacks the committed tree (not `.env`, not uncommitted files) as `/opt/muse/releases/0.0.0+<commit>`, then copies this checkout's updater into that tree so the timer can install a signed release later (the container does not mount `deploy/`);
- writes `/etc/muse/vm.conf` with the updater off until you paste the public key, and with `MUSE_IDLE_GUARD=off`;
- installs the update timer, the heartbeat timer, and the sampler;
- starts the same compose project name (`muse-stack`) from the new directory.

`MUSE_UPDATE_PUBKEY` is left empty on purpose. Paste the public key, then the updater is willing to run:

```bash
sudoedit /etc/muse/vm.conf
# MUSE_UPDATE_PUBKEY=<the line generate printed>
```

Check the proxy:

```bash
curl -fsS http://127.0.0.1:4000/health/liveliness
sudo bash /opt/muse/current/deploy/vm/muse-update.sh
```

With no signed release newer than `0.0.0`, that run reports that it is up to date or that the manifest is missing. Either is fine. It must not print the env file.

### Go back to the clone

The clone and its `.env` are still at `/opt/muse-stack`.

```bash
sudo bash /tmp/muse-tools/deploy/vm/migrate-from-clone.sh --rollback --yes --clone /opt/muse-stack
```

That starts compose from the clone again. It does not delete `/opt/muse` or `/etc/muse/muse.env`. Keep `/tmp/muse-tools` until the first signed update: until then, that checkout is the copy of the updater and of this rollback command. To return to the signed layout, stop the clone's project and start `/opt/muse/current`.

## 5. Turn the timer on

Migration enables it when systemd is available. If you skipped that:

```bash
sudo systemctl enable --now muse-update.timer
sudo systemctl start muse-update.service
journalctl -u muse-update -n 50 --no-pager
```

The timer is 03:00 UTC plus up to 15 minutes, and it catches up after the VM was off. A second run while one is going exits immediately and does not touch the first.

## 6. Healthchecks

Create a check with a period of one day and a grace of about six hours. Put the ping URL, alone, in `/etc/muse/healthchecks.url` (mode 0600, root). The name of that file is `MUSE_HEALTHCHECKS_URL_FILE` in `vm.conf`. The URL is not written anywhere in the repository.

```bash
sudo install -m 0600 /dev/null /etc/muse/healthchecks.url
sudoedit /etc/muse/healthchecks.url
```

The updater pings success when it is up to date or has switched, and failure when a check or a release fails. It does not ping while updates are still disabled (no public key), so create the check after the first manual run you care about, or expect the check to sit quiet until then.

A second check, every five minutes with about fifteen minutes of grace, goes in `/etc/muse/heartbeat.url`. Then:

```bash
sudo systemctl enable --now muse-heartbeat.timer
```

Without that file the timer starts and does nothing.

## 7. What a night-time run does

1. Fetches `stable.json` and `stable.json.sig` over https only.
2. Checks the Ed25519 signature against `MUSE_UPDATE_PUBKEY` (and `MUSE_UPDATE_PUBKEY_NEXT`, if you are rotating). Refuses a bad signature, a branch name, a non-https URL, a bad checksum, an expired manifest, or a manifest dated more than 90 days out.
3. Refuses a version older than the one running.
4. Downloads the tarball and checks its SHA-256.
5. Takes a lock so a second run cannot start.
6. Snapshots `/var/lib/muse/home` (SQLite online backup).
7. `docker compose pull`, runs `validate_config.py` inside the image, then `docker compose up -d`. The image digest is the one in the release's compose file.
8. Waits for `/health/liveliness`, then asks for `/v1/models` with the master key, then makes one short `fast` call.
9. On success, points `current` at the new release and keeps the previous tree. On failure, starts the previous release and restores the snapshot.

`MUSE_UPDATE_PIN=0.2.0` in `vm.conf` makes every other version a no-op. Use it when you have rolled back and do not want the next night to move forward again.

## 8. Roll back a release

Automatic: a failed smoke test already did it. `last.json` says `rolled-back`.

By hand, back to the release the last successful update came from. The command exits non-zero on purpose, including when the previous release is up again, so the rollback is visible. `last.json` says `rolled-back`.

```bash
sudo /opt/muse/current/deploy/vm/muse-update.sh --rollback
sudoedit /etc/muse/vm.conf
# MUSE_UPDATE_PIN=<the version you just returned to>
```

That restores the data snapshot taken before the last successful update. It does not delete the newer tree. The next night will install the newer release again unless the pin is set.

If the whole signed layout is wrong, use the clone rollback in section 4.

## 9. Rotate the signing key

Do this in one sitting. Between the two steps the VM will refuse manifests signed by the key it does not have yet, and it will stay on the release it already has.

1. `sign_manifest.py generate --dir` somewhere new. Do not overwrite the old key until a release signed by the new key is published.
2. Set `MUSE_UPDATE_PUBKEY_NEXT` on the VM to the new public line. Leave `MUSE_UPDATE_PUBKEY` as the old one. The VM accepts either.
3. Sign and publish a manifest (a refreshed expiry is enough) with the new private key.
4. Confirm `journalctl -u muse-update` shows an up-to-date or updated run, not a refusal.
5. Move the new public line to `MUSE_UPDATE_PUBKEY`, delete `MUSE_UPDATE_PUBKEY_NEXT`, and delete the old private key.

## 10. Idle reclaim

Oracle may reclaim an Always Free instance when, over seven days, CPU (95th percentile), network, and on this ARM shape memory are all under 20%. A nightly update and a five-minute ping are not enough to stay above that.

Prefer upgrading the tenancy to Pay As You Go. The Always Free allowances still apply, and idle reclaim stops. You do not have to burn CPU, and you do not have to guess what the terms think of it.

The other option is the idle guard, and it is off after migration. It is deliberate processor use so the 95th percentile stays near 25%: about one minute in ten, one thread, lowest priority, and it stops as soon as real work shows up. It does not allocate memory and it does not send traffic. Oracle's Always Free page states the reclaim rule and does not say whether staying above it on purpose is allowed. That is the tradeoff. If you accept it:

```bash
sudoedit /etc/muse/vm.conf
# MUSE_IDLE_GUARD=on
sudo systemctl enable --now muse-vm-monitor
sudo systemctl restart muse-vm-monitor
```

`MUSE_IDLE_GUARD=auto` follows the account type (on for Always Free, off for paid). The migration writes `off`, not `auto`.

## 11. First install on a new VM

Same layout, without a clone to copy. You still need Docker, `python3-cryptography`, an env file you write yourself (mode 0600), and a release tarball you have already checked.

```bash
sudo install -d -m 0755 /opt/muse/releases /etc/muse
sudo install -d -m 0700 /var/lib/muse /var/lib/muse/home /var/lib/muse/vm
sudo install -m 0600 /path/to/your.env /etc/muse/muse.env
# unpack muse-<version>.tar.gz so the files land in /opt/muse/releases/<version>/
sudo ln -sfn /etc/muse/muse.env /opt/muse/releases/<version>/.env
sudo ln -sfn releases/<version> /opt/muse/current
```

Write `/etc/muse/vm.conf` the way the migration does (`MUSE_RUNTIME=docker`, the paths above, the public key, `MUSE_IDLE_GUARD=off`). Then:

```bash
sudo bash /opt/muse/current/deploy/vm/install-services.sh \
  --user ubuntu --root /opt/muse/current --venv /usr \
  --state /var/lib/muse --base /opt/muse --conf /etc/muse/vm.conf \
  --units "muse-update muse-heartbeat muse-vm-monitor" --enable
sudo docker compose -p muse-stack --project-directory /opt/muse/current \
  --env-file /etc/muse/muse.env up -d
```

Caddy, if you use it, keeps proxying to `127.0.0.1:4000`. This layout does not open that port.

## 12. What you should not do

- Do not commit `.env`, `release.key`, or a healthchecks URL.
- Do not point a self-hosted runner at this VM. The runner's account can read the env file.
- Do not `git pull` inside `/opt/muse/current`. The next update replaces that directory from the signed tarball.
