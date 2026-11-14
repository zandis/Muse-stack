# Oracle Cloud Always Free (ARM, US West / San Jose)

The first host can be any small Linux box with Docker. This note is the later move to an Oracle Cloud Always Free Ampere VM in `us-sanjose-1`.

Two ways to set the VM up:

- **One button, no terminal:** [deploy/oracle-stack/README.md](oracle-stack/README.md). A Resource Manager stack creates the VM (2 OCPU and 12 GB), a network with no inbound rule, and a private bucket for encrypted backups; first boot installs a pinned, checksum-verified Muse release. Not yet tried on a real account.
- **By hand, over SSH:** the rest of this page and `deploy/oracle-a1-setup.sh`.

## Shape

- Image: Ubuntu 22.04 or 24.04, **aarch64**. Canonical's Ampere images are the ones that match.
- Shape: `VM.Standard.A1.Flex`. The Always Free allowance is 2 OCPU and 12 GB of memory (1,500 OCPU hours and 9,000 GB hours a month), shared by every A1 instance in the tenancy; it was 4 and 24 before mid-2026, and an instance larger than 2 / 12 on a free-only account is no longer covered. Source: Oracle's [Always Free Resources](https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm) page, read on 13 November 2026. Capacity in popular regions is often unavailable; 1 OCPU and 6 GB is enough for this proxy and is a reasonable thing to request if a larger shape fails with "out of host capacity".
- The LiteLLM image in `docker-compose.yml` is `ghcr.io/berriai/litellm:v1.108.6`, pinned by the digest of its multi-arch index, which includes `linux/arm64` (`sha256:91addd1d9a0ee27b8f3202472915e41e6c019c984e590af9d81f613b9fe99742`). Do not switch it to `docker.io/litellm/litellm`: those tags are amd64-only and the pull fails on this VM with `no matching manifest for linux/arm64`.
- Postgres in `docker-compose.spend.yml` uses `postgres:16-alpine`, which is also multi-arch. Skip that file until you actually want spend logs. The proxy does not need a database to route.

## Network

San Jose is `us-sanjose-1`. A client in Taiwan will see roughly an extra ocean round trip on top of the provider's own latency. That is fine for an interactive side project and a poor fit for anything that streams token-by-token to a user sitting next to the data.

Prompts that reach this VM have already left Taiwan, and from here they go to whichever provider the router picks (Google, Groq, OpenRouter, and the others). Do not put patient-identifiable data on this path. Free tiers log prompts or train on them. See the README warning.

Oracle's default security list leaves port 22 open more widely than you want. Restrict SSH to your own IP. Do not add port 4000 to the security list. The compose file binds the proxy to `127.0.0.1`. Put Caddy or nginx on the VM for HTTPS and proxy to `127.0.0.1:4000`, and only then open 443. Set `LITELLM_MASTER_KEY` before that.

## Scripted setup

`deploy/oracle-a1-setup.sh` does the steps below in one run, on loopback only:
Docker (or `--native` for a venv with no Docker), a `.env` with mode 600 and a
generated master key, the proxy on `127.0.0.1:4000`, the `muse` CLI, and the
dashboard on `127.0.0.1:4180` as a user service. It opens no port and never
prints a key. It has not been tested on a real Oracle VM; read it first.

The repository is private, so the VM needs its own read-only access to clone
it: create a key on the VM (`ssh-keygen -t ed25519`) and add the public half
under the repository's Settings, Deploy keys, without write access.

Always Free note: Oracle may reclaim an instance it considers idle: over 7 days,
CPU (95th percentile), network and memory use all under 20%. The stack's idle
guard and what Oracle's page does and does not say about it are described in
[deploy/oracle-stack/README.md](oracle-stack/README.md), "Idle guard".

`./deploy/oracle-a1-setup.sh --services` also installs the background services
the stack uses (`deploy/vm/units`): the home control API on a Unix socket, the
queue, a timer that runs `muse chores tick` every minute, the weekly catalogue
check, and the sampler with the idle guard. They are hardened system units that
run as you. Without the flag the script does exactly what it did before.

## Boot

```bash
sudo apt-get update
sudo apt-get install -y docker.io docker-compose-v2 git
sudo usermod -aG docker "$USER"
# log out and back in so the group applies

git clone https://github.com/zandis/muse-stack.git
cd muse-stack
cp .env.example .env
printf 'LITELLM_MASTER_KEY=sk-%s\n' "$(openssl rand -hex 32)" >> .env
# paste provider keys into .env, or leave them blank

docker compose up -d
docker compose ps
curl -fsS http://127.0.0.1:4000/health/liveliness
python3 scripts/smoke_test.py
```

`python3 scripts/smoke_test.py` calls each alias that the running config actually contains and skips the rest. With no provider keys it only checks that the proxy is up.

Logs:

```bash
docker compose logs -f litellm
```

The container prints the enabled providers on startup. A missing key is a line in that list, not a crash.

## Updates

The pin stays on `v1.108.6` and its digest until you edit the compose file, so a pull can only ever fetch that exact image:

```bash
docker compose pull
docker compose up -d
```

### Supply chain

What runs on this VM is fixed, because LiteLLM published a malicious release on 21 October 2026 (`litellm==1.107.0` and the tags `latest`, `main-latest`, `main-stable`, validly signed with their old key). The Docker path pulls `ghcr.io/berriai/litellm:v1.108.6@sha256:c64fe10f82a1f77906f88574f954317c50f67c617e0c97b70ac8fc61b74d3ec3`; never change it to a floating tag. The `--native` path installs from `requirements/proxy.lock` with `pip install --require-hashes`, so pip refuses any file whose SHA-256 is not in the repo, and the setup script then runs `scripts/verify_litellm.py` and stops if it reports a problem. To check at any time, on the VM: `python3 scripts/verify_litellm.py --image` (Docker; asks ghcr.io whether the tag still points at the pinned digest) or `~/.muse-venv/bin/python scripts/verify_litellm.py` (native, the venv the script created); `docker compose images` shows the digest in use. It reads no key; exit status 0 is clean, 1 a problem, 2 something it could not check. To move to a newer LiteLLM, do it on your own machine first, in the order given in the README under "Supply chain" (new tag, resolve its digest, regenerate the lock files, run the suite), then `git pull` here and run the setup script or `docker compose pull && docker compose up -d` again. Provider keys live only in the proxy's environment on this VM (`.env`, mode 600): not in an application, not in the repository, not in CI.

If you enable `docker-compose.spend.yml`, keep the same `LITELLM_SALT_KEY` across recreates. Changing it makes database-stored credentials unreadable. The routing table itself is the rendered config, not the database.
