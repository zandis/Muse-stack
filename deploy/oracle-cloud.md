# Oracle Cloud Always Free (ARM, US West / San Jose)

The first host can be any small Linux box with Docker. This note is the later move to an Oracle Cloud Always Free Ampere VM in `us-sanjose-1`.

## Shape

- Image: Ubuntu 22.04 or 24.04, **aarch64**. Canonical's Ampere images are the ones that match.
- Shape: `VM.Standard.A1.Flex`. Always Free quota is up to 4 OCPU and 24 GB RAM, shared by every A1 instance in the tenancy. Capacity in popular regions is often unavailable; 1 OCPU and 6 GB is enough for this proxy and is a reasonable thing to request if a larger shape fails with "out of host capacity".
- The LiteLLM image in `docker-compose.yml` is `ghcr.io/berriai/litellm:v1.108.5`, which publishes `linux/arm64`. Do not switch it to `docker.io/litellm/litellm`: those tags are amd64-only and the pull fails on this VM with `no matching manifest for linux/arm64`.
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

Always Free note: Oracle may reclaim an instance it considers idle. Check the
current rule in Oracle's Always Free documentation.

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

Pin stays on `v1.108.5` until you edit the compose file. Pull and recreate:

```bash
docker compose pull
docker compose up -d
```

If you enable `docker-compose.spend.yml`, keep the same `LITELLM_SALT_KEY` across recreates. Changing it makes database-stored credentials unreadable. The routing table itself is the rendered config, not the database.
