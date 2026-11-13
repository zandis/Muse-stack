# VM test plan

Written 2026-11-13. This is the list of things that can only be learned on a real machine with real keys, in the order worth doing them, with the exact commands. It is written for the owner's VM `muse-a1` (Ubuntu 24.04, ARM, Oracle Cloud), where the live router runs.

Why the VM matters: every test in the repository uses scripted replies. The first real run found a serious defect that about 3,000 tests had missed. Real runs are the only evidence that counts for gate 1.

## What is already known from the VM

| Date | Run | Result |
|---|---|---|
| 2026-11-12 | Real-key check, 9 requests | Routing, privacy refusal, app tokens and background yielding work. Reasoning models return nothing at a 16-token cap |
| 2026-11-13 | Gate 1 sample, 10 runs | Files skill 4 of 5 right; research report 0 of 5 |
| 2026-11-13 | Gate 1 full, 133 runs, 922 requests, US$0 | Files skill 65 of 67 right. Research report 27 of 66; 22% of its calls empty. The day's free allowance ran out after about 900 requests |

## Layout on the VM, as reported

- **Live tree** `/opt/muse-stack`: last reported at commit `6aefbb8`, far behind the work branch. Its `muse/`, `scripts/` and `config/` folders are mounted into the running LiteLLM container.
- **Side checkout** `/home/ubuntu/muse-stack-gate1`, with its own environment, used for the gate runs.
- **State** `/home/ubuntu/.muse` (ledger, apps, reports).

Rules that stay in force on this VM: no real patient data; test network only; every service bound to `127.0.0.1`; secrets entered on the VM only; nothing merged or force-pushed from it.

---

## Stage 0: make real runs routine (do once, about 30 minutes)

**0.1 Check the account type and what the VM costs.** The VM has been described as 4 cores with 24 GB and as 4 cores with 48 GB; the console decides. Do not press Upgrade during this check.

1. In the Oracle console, open the profile menu, then Tenancy. Note whether the account is "Always Free" or "Pay As You Go", and the home region.
2. Open Compute, Instances, the instance. Note the shape, the OCPU count and the memory.
3. Open Billing, Cost Analysis, last 7 days, filtered to this instance. Note the amount.
4. Take a boot-volume backup (Block Storage, Boot Volumes, the volume, Create Backup). Five backups are free.

What the answers mean:

| Account | Size | Meaning |
|---|---|---|
| Always Free | 2 cores, 12 GB or less | Inside the free allowance. It can still be stopped after 7 quiet days (see below) |
| Always Free | more than 2 cores or 12 GB | Over the documented allowance. It can be stopped, and the larger size may not be available again. Ask Oracle Support in writing before resizing or upgrading |
| Pay As You Go | up to 4 cores, 24 GB | Reported to be free inside the paid-account allowance of 3,000 core-hours and 18,000 GB-hours a month. A card is on file, so set a budget alert first. This figure comes from the review of 14 November and has not been re-read by me on Oracle's price list |
| either | 48 GB | Above every free allowance. Cost Analysis will show what it costs |

Oracle may stop an Always Free instance when, over 7 days, processor use (95th percentile), network use and, on ARM, memory use are all under 20%. All three must be low, so keeping one of them above 20% is enough. The repository's idle guard does this with the processor (`MUSE_IDLE_GUARD`, target 25%); it is off until you decide. See `docs/reviews/vm-options-2026-11-14.md`.

**0.2 Let Claude work on the VM.** This removes the relay through you.

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude            # sign in once; press c to copy the link and open it on your own computer
tmux new -s rc
cd /home/ubuntu/muse-stack-gate1
claude remote-control --name "muse-a1"
# detach: Ctrl+b then d
```

Then tell me it is running. If this session cannot attach to it, open the "muse-a1" session from the Claude app and paste this plan into it.

**0.3 Install the sandbox layer and tools used below.**

```bash
sudo apt-get update && sudo apt-get install -y bubblewrap age
```

---

## Stage 1: bring the live router up to date (15 minutes, at a quiet moment)

The gate runs so far used current skills against an old router. Several fixes are router-side: the output floor for reasoning models, answering a rate limit at once, `Retry-After` on provider limits.

**1.1 Record where you are, so you can go back.**

```bash
cd /opt/muse-stack
git rev-parse HEAD | tee ~/muse-live-before.txt
git status --short | head
docker compose ps
cp .env ~/muse-live-env-backup && chmod 600 ~/muse-live-env-backup
```

**1.2 Update and restart.**

```bash
git fetch origin ccr-645e0264-bn082z
git checkout ccr-645e0264-bn082z && git pull --ff-only
python3 scripts/validate_config.py | tail -1        # must print: config ok
docker compose pull
docker compose up -d
sleep 20 && curl -fsS http://127.0.0.1:4000/health/liveliness
```

The compose file now names the image by digest. If `docker compose pull` reports a digest mismatch, stop and tell me; do not work around it.

**1.3 Check the installation is the expected one.**

```bash
python3 scripts/verify_litellm.py --image
ss -tlnH | awk '{print $4}' | sort -u               # every line must start with 127.0.0.1 or [::1], except sshd
```

**1.4 Confirm the new behaviour with one tiny request.** Replace `MASTER` by reading it on the VM; do not paste it anywhere.

```bash
set -a; . ./.env; set +a
curl -s -D - -o /dev/null http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $LITELLM_MASTER_KEY" -H "content-type: application/json" \
  -d '{"model":"fast","messages":[{"role":"user","content":"Reply with the single word: ok"}],"max_tokens":16}' \
  | grep -i "^x-muse"
```

Expected: `x-muse-provider`, `x-muse-task` or `x-muse-app`, and, if a reasoning model answered, `x-muse-min-output: raised:16->…`. If no `x-muse-min-output` or `x-muse-answer` header can ever be produced, the container is still running old code.

**To go back:** `git checkout $(cat ~/muse-live-before.txt) && docker compose up -d`.

**Send me:** the output of 1.3 and the header lines of 1.4.

---

## Stage 2: checks that need ARM, not keys (20 minutes, no requests sent)

These settle things I could not verify on the build machine, which is not ARM.

**2.1 The hash-locked install works on ARM.**

```bash
cd /home/ubuntu/muse-stack-gate1 && git pull --ff-only
python3 -m venv ~/venv-lock && . ~/venv-lock/bin/activate
pip install --require-hashes -r requirements/proxy.lock -r requirements/dev.lock 2>&1 | tail -3
pip install --no-deps -e . && pip check
python scripts/verify_litellm.py
```

A failure here names a package with no ARM build. That blocks the one-button VM setup.

**2.2 The sandbox really isolates on this kernel.**

```bash
python -m muse doctor | grep -i -A3 sandbox
python -m pytest tests/test_sandbox_escape.py tests/test_sandbox.py -q -rs 2>&1 | tail -15
```

Run as the ordinary `ubuntu` user, not root. If doctor shows `setting up uid map: Permission denied`, Ubuntu's AppArmor rule is blocking the outer layer; the other two layers should still satisfy "require". Send me the doctor lines and the last 15 lines.

**2.3 The full test suite and the Rust core on ARM.** About 20 minutes; it can run in tmux.

```bash
python -m pytest tests -q -p no:cacheprovider 2>&1 | tail -5
curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs | sh -s -- -y && . ~/.cargo/env
(cd core && cargo test --locked 2>&1 | grep "test result")
```

**Send me:** the last lines of each.

---

## Stage 3: the gate, on the fixed skills (after I confirm the research fix is merged)

Do not re-run before I say the fix is merged; the result would be the same as today.

**3.1 Files skill only, capped.** The files skill passed (65 of 67). This run looks at its two misses and its slow tail, with failed answers kept.

```bash
cd /home/ubuntu/muse-stack-gate1 && git pull --ff-only && . ~/venv-lock/bin/activate
python scripts/chores_check.py --check-tasks
python scripts/chores_check.py --only files-answer --keep-failures --max-requests 200
```

`--keep-failures` and `--max-requests` arrive with the fix. Until then use `--keep-outputs` and `--sample 20`.

**3.2 Research report, a sample first.**

```bash
python scripts/chores_check.py --only research-report --sample 10 --keep-failures --max-requests 250
```

Go on to the full 66 only if at least 7 of the 10 are right by expected facts, and do it on a day when you do not need the allowance yourself: the full set used about 770 requests.

**What passes:** at least 80% of completed runs **right by expected facts**, nothing spent, on at least 50 runs. The skill's own check is reported beside it and does not decide the gate.

**Send me:** the report JSON, the CSV, and `chores-failures.jsonl`. None of them contains keys or your documents; the failures file holds the model's answers and the quotes it used from the fictional test documents.

---

## Stage 4: which free models are good at Traditional Chinese (about 120 requests)

```bash
muse apps add eval --priority background        # prints a key once; store it on the VM only
export MUSE_EVAL_APP_TOKEN=...                  # type it; do not paste it into chat
python scripts/eval_models.py --sample 30 --seed 1 --deployments --suggest
```

This ranks each connected model on short checkable tasks. It prints suggested routing lines and changes nothing. The test set was written by a model and has not been read by a native Taiwan reader, so treat the ranking as rough.

**Send me:** `eval-report.json`.

---

## Stage 5: a nightly run (set up once)

A small capped run every night, so regressions and provider changes show up within a day.

```bash
mkdir -p ~/.config/systemd/user
cat > ~/.config/systemd/user/muse-nightly.service <<'EOF'
[Unit]
Description=Muse nightly real run (capped)
[Service]
Type=oneshot
WorkingDirectory=/home/ubuntu/muse-stack-gate1
ExecStart=/bin/bash -lc '. ~/venv-lock/bin/activate && git pull --ff-only && python scripts/chores_check.py --only files-answer --sample 20 --yes --keep-failures --max-requests 120 --report ~/.muse/nightly-report.json --csv ~/.muse/nightly-runs.csv'
EOF
cat > ~/.config/systemd/user/muse-nightly.timer <<'EOF'
[Unit]
Description=Muse nightly real run
[Timer]
OnCalendar=*-*-* 15:00:00 Asia/Taipei
Persistent=true
[Install]
WantedBy=timers.target
EOF
systemctl --user daemon-reload && systemctl --user enable --now muse-nightly.timer
sudo loginctl enable-linger ubuntu
```

120 requests is about an eighth of the daily allowance measured on 13 November. Gemini's daily allowance refills at midnight Pacific, which is 16:00 in Taipei in winter (15:00 in summer). Running at 15:00 Taipei uses what would otherwise expire an hour later and leaves the new day untouched. Move the time to 14:00 when Pacific daylight time starts in March. The side checkout follows the work branch; the live router does not change by itself.

---

## Stage 6: a host bot on your own machine (optional, 30 minutes)

This tests the host adapter with a real bot. Do it on your own computer or the VM, not both.

```bash
muse home token list                    # remove any host token made before 13 November: muse home token rm <name>
muse hosts setup openmausbot            # prints the settings to paste; writes nothing into the bot's files
muse home serve &                       # the home API on a private socket
```

Then follow `docs/hosts/openmausbot.md`. Every host-side step in that guide is marked "not run here": this would be its first real run. Check afterwards:

```bash
muse hosts check openmausbot ~/.openmausbot/config.json
muse records list --since 1h
```

What to look for: the bot's answers appear in Muse's records with a route; a request for approval from the bot shows up as a Muse card; and the bot's token cannot resume or store a key (`muse home call POST /v1/resume` with the host token must be refused).

---

## Stage 7: the one-button VM setup on a real Oracle account (later; needs decisions first)

Not ready to run. It needs, from you: a public address for the release files (the repository is private, and both Oracle's deploy button and the first boot fetch over plain https), and a decision on the idle guard being on by default. When those exist, the ten checks are listed in `deploy/oracle-stack/README.md`; the first four gate the rest:

1. Terraform validates and applies through Oracle's Resource Manager.
2. The deploy button accepts the published zip address.
3. The instance boots with no SSH key and finishes setup in under 10 minutes.
4. Every locked dependency has an ARM build (stage 2.1 answers this early).

---

## What not to do on the VM

- Do not run the full 133-task set on a day you need the allowance. It uses nearly all of it.
- Do not run `--probe-limits`. It deliberately exhausts a provider.
- Do not set `MUSE_PERMISSIONS=enforce` or put anything real in a vault. Both are unreviewed.
- Do not open a port, and do not bind anything to a public address.
- Do not paste keys, `.env` values or tokens into chat or a pull request. Reports and CSVs are safe to send.

## Order, at a glance

| Stage | Needs keys | Sends requests | Time | Do it |
|---|---|---|---|---|
| 0 Routine | No | No | 30 min | Now |
| 1 Update the live router | Yes | 1 | 15 min | Now, at a quiet moment |
| 2 ARM checks | No | No | 20 min + suite | Now |
| 3 Gate on fixed skills | Yes | 200–450 | 20–60 min | After I confirm the fix is merged |
| 4 Chinese ranking | Yes | about 120 | 10 min | Any day with spare allowance |
| 5 Nightly | Yes | 120 a night | 10 min once | After stage 3 |
| 6 Host bot | Yes | A few | 30 min | Optional |
| 7 One-button VM | Oracle account | No | Half a day | Later |
