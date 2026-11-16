# Wardmuse under OpenMausBot

Written 13 November 2026. Read [README.md](README.md) first: it says what Wardmuse does and does not control under a host bot.

**Read this before you rely on it.** OpenMausBot was not installed in the place this guide was written. Its configuration field names were read on its own pages that day:

- <https://github.com/milind-soni/OpenMausBot/blob/main/docs/custom-engines.md>
- <https://github.com/milind-soni/OpenMausBot/blob/main/docs/custom-mcp-servers.md>
- <https://github.com/milind-soni/OpenMausBot/blob/main/README.md>

Nothing here was tried end to end. Each step says whether it was run here. "Run here" means the Wardmuse command was run in tests, against a fake home where a home is needed. "Not run here" means nobody has seen it work.

## What you need

- Wardmuse set up and running (`muse up`, `muse status` shows your services).
- OpenMausBot installed.
- The `mcp` Python package in the same Python that runs Wardmuse (`pip install -e '.[mcp]'` in the Wardmuse repository).

## Step 1: create the host's key and tools token

Run here: yes, the Wardmuse command. In a terminal:

```bash
muse hosts setup openmausbot
```

It does three things:

1. Creates a Wardmuse app called `openmausbot`: interactive priority, no daily caps, not allowed to pay. Its key is shown once. Copy it now.
2. Creates a device called `openmausbot-tools` for the tools server, with the role `host`, and writes its token to `~/.muse/host-openmausbot.token`, a file only you can read. That token is not printed. A `host` token can do only what the tools server needs (see below).
3. Prints the two blocks for step 3 and step 4.

It does not touch OpenMausBot's files. `muse hosts show openmausbot` prints the blocks again later, without the key.

## Step 2: keep the home running

Run here: yes, against the fake home only. The tools server talks to Wardmuse's home, so the home must be up:

```bash
muse home serve
```

Leave it running. Without it the tools answer "Wardmuse's home is not running" and the engine door still works.

## Step 3: add the engine

Not run here. OpenMausBot's documentation says engine entries live under `"instances"` in `~/.openmausbot/config.json`. Add this entry (keep whatever is already in the file), and put the key from step 1 where it says so:

```json
{
  "instances": {
    "muse": {
      "driver": "openai-compat",
      "displayName": "Wardmuse",
      "environment": { "MUSE_APP_KEY": "<paste the Wardmuse app key here>" },
      "config": {
        "url": "http://127.0.0.1:4000/v1",
        "apiKeyEnv": "MUSE_APP_KEY",
        "model": "auto"
      }
    }
  }
}
```

- `"apiKeyEnv"` names which `"environment"` value carries the key. The name is `MUSE_APP_KEY`.
- `"model": "auto"` lets Wardmuse pick by task. The other names are `fast`, `cheap`, `smart` and `long-context`.
- OpenMausBot keeps `"environment"` values as plain text in that file, which it writes so only you can read it. That is why the key is an app key of the host's own: `muse apps rotate openmausbot` replaces it without touching anything else.
- If OpenMausBot runs in Docker, `127.0.0.1` is the container, not your computer. Use `host.docker.internal`.

Its documentation says to restart the app after editing this file.

## Step 4: add the Wardmuse tools server

Not run here. Two ways, both from OpenMausBot's documentation.

In the app: **Plugins → MCP servers → Add server → Run a command**. The executable and each argument are the ones `muse hosts setup` printed.

Or in the same file, under `"mcpServers"`:

```json
{
  "mcpServers": {
    "muse-tools": {
      "command": "/path/to/python",
      "args": ["-m", "muse.mcp", "--token-file", "/home/you/.muse/host-openmausbot.token"]
    }
  }
}
```

Use the exact `"command"` and `"args"` that `muse hosts setup` printed: the full path of the Python that has Wardmuse, and the full path of the token file. A desktop app started from the dock often does not have your shell's `PATH`.

No token goes into OpenMausBot's file for this: the tools server reads it from the private file. Its documentation says a server you add is tested and then switched on, and that tools from a server you added show Allow / Deny cards by default.

Not verified: whether the name `muse-tools` is free. Its documentation lists some reserved names, not all. If it is refused, pick another.

## Step 5: check what goes through Wardmuse

Run here: yes, on sample files written from the documented format. Not run on a real OpenMausBot file.

```bash
muse hosts check openmausbot ~/.openmausbot/config.json
```

It only reads the file. It says whether an engine points at Wardmuse's address and sends a Wardmuse app key, whether the tools server is registered, and it warns about every engine that does not go through Wardmuse. It never prints a key it finds.

It cannot see one thing. OpenMausBot offers the `claude`, `codex` and `grok` command-line tools by themselves when they are installed and signed in, and they do not appear in this file. Those use your own subscription login; Wardmuse is not involved in them and cannot ration or record them. **In each bot's model picker, choose the engine called Wardmuse.** A bot left on another engine is outside Wardmuse entirely.

## Step 6: ask one question and see it in Wardmuse

Not run here.

1. In OpenMausBot, pick a bot whose engine is Wardmuse and ask it something short.
2. In a terminal, run `muse status` (or open the dashboard with `muse dash`). Under applications, `openmausbot` should show one request today. Under recent calls, the newest line should show which service and model answered.

To see the route facts themselves, make one call by hand with the same key. Put the key in an environment variable first so it stays out of your shell history:

```bash
read -rs MUSE_APP_KEY && export MUSE_APP_KEY      # paste the key, press Enter
curl -si http://127.0.0.1:4000/v1/chat/completions \
  -H "Authorization: Bearer $MUSE_APP_KEY" -H "Content-Type: application/json" \
  -d '{"model": "auto", "messages": [{"role": "user", "content": "Say hello."}]}' | grep -i '^x-muse-'
```

The reply headers `x-muse-provider`, `x-muse-deployment` and `x-muse-privacy` say where the call went. A call that Wardmuse turned away is HTTP 429 with `x-muse-ration`, and the message ends with the choices available now.

Whether OpenMausBot shows those headers anywhere itself is not known. Wardmuse's own status and dashboard are where to look.

## Step 7: try the tools

Not run here.

Ask the bot: "Use the Wardmuse tools to tell me what allowance is left." It should call `allowance_status`. Then ask it to save a small file and to ask you through Wardmuse first. A card should appear in Wardmuse (`muse home call GET /v1/cards?status=pending` shows it until the app exists). Decline it, and the bot is told not to do it.

Remember what that proves and what it does not: the bot asked because it chose to. Nothing in Wardmuse stops OpenMausBot's own shell from writing the file without asking.

## What is and is not protected

| Protected by Wardmuse | Not protected by Wardmuse |
|---|---|
| Model calls from bots whose engine is Wardmuse: rationing, privacy classes, the paid cap | Model calls from bots on any other engine, including the built-in command-line engines |
| Paid use: the `openmausbot` app may not pay until you run `muse apps set openmausbot --allow-paid` | What OpenMausBot's bots do with their own shell and file tools, with or without its "Full access" switch |
| Actions a bot asks about through `ask_approval` and waits for | Actions a bot does not ask about |
| Queued jobs: held while Wardmuse is paused; paid only with an approved spend card | OpenMausBot's connected apps, which go through a third-party cloud service |

Three more things to know:

- The Wardmuse app key sits in OpenMausBot's configuration file as plain text. Anything that can read that file can use your free allowance as `openmausbot`, up to that app's limits. Set limits with `muse apps set openmausbot --daily-tokens N`.
- The tools token sits in `~/.muse/host-openmausbot.token`. A bot that can run commands as you can read it. That is why it is a `host` token and not a full one. With it a program can read Wardmuse's status, ask you questions and read the ones it asked, queue jobs and read or cancel the ones it queued, add records, and list and run skills (reading only folders you put on a list with `muse skills folders add`, by name). It **cannot** pause or resume Wardmuse, store a key, connect a service, see or change apps, answer any approval card, read your records, or see another device's jobs, cards or skill runs. The home refuses those calls itself; `muse home token list` shows the role. If you ran `muse hosts setup` before roles existed, the token it made then can do everything: remove it and run setup again ("How to undo" below, then step 1).
- OpenMausBot's own security notes describe agents running with your privileges. Wardmuse does not change that.

## How to undo

```bash
muse apps rm openmausbot                  # the engine key stops working at once
muse home token rm openmausbot-tools      # the tools token stops working at once
rm ~/.muse/host-openmausbot.token
```

Then remove the `"muse"` entry under `"instances"` and the `"muse-tools"` entry under `"mcpServers"` from `~/.openmausbot/config.json` (or remove the server in Plugins → MCP servers) and restart OpenMausBot. Not run here.

## Not verified

- Everything on OpenMausBot's side: that the engine entry loads, that the tools server passes its start-up test, that tool results display, that its schedules work with Wardmuse.
- How well its agent loop works with the free models behind Wardmuse's aliases. Its documentation has a `"config.tools": false` setting for models without tool support.
- Whether `auto` appears in its model list. Its documentation says a configured model stays selectable even when the list cannot be fetched.
