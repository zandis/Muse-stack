# Running Muse under a bot you already use

Written 13 November 2026. The design is in [spec part 5](../spec/05-host-bots.md).

## What a host is

A **host** is a bot or bot app that already exists and that you already like: it has its own chat window, its own agent loop, its own schedules. Muse does not replace it. Muse sits underneath it and does the things the host does not do: ration your free AI allowances, cap what you spend, and put a question in front of you before something is written, sent or paid for.

Muse has its own thin helper too. That is the default for people who do not want to set up a bot. A host is for people who already run one.

## The two doors

A host reaches Muse through two doors and nothing else.

| Door | What the host gets | How it is set up |
|---|---|---|
| **Engine** | Muse's OpenAI-compatible address, `http://127.0.0.1:4000/v1`, with an app key of the host's own. Every model call is rationed, follows your privacy classes, and stays under your paid cap. A call that is turned away says why and what the choices are. | The host's "custom OpenAI-compatible engine" setting |
| **Tools** | The Muse tools server, `python -m muse.mcp`. The host's bots can ask what is left, ask you for approval, and queue work for later. | The host's "MCP server" setting |

`muse hosts setup <name>` creates what both doors need and prints exactly what to paste into the host. Muse never writes into a host's files.

**The rule about logins.** Muse connects to a host only through the OpenAI-compatible engine setting, never through a vendor's command-line login. Many hosts can also drive a vendor's own command-line tool under your personal subscription. Muse is not part of that path: it cannot ration it, cap it or record it, and it never will. `muse hosts check` reads the host's configuration and warns when it sees such engines.

## What Muse enforces, and what it does not

| Helper | For whom | What Muse enforces |
|---|---|---|
| **Muse's own thin helper** | People who do not run a bot; the default | Everything in the Muse design |
| **A host bot** | People who already run one | Model use that goes through the Muse engine. Spending through Muse. Anything done through Muse's tools. **Not** what the host does with its own shell and file tools |

Said once more, because it matters:

> Muse controls model use, spending and its own tools; it cannot stop this host's own shell or file tools.

Under a host bot:

- A bot that asks through `ask_approval` and waits is held by your answer. A bot that does not ask is not held by Muse at all. The host's own Allow and Deny cards are still the only thing between its shell and your files.
- Muse's records show what went through Muse, and nothing else. What a bot reports with `record_action` is written down as that bot's own report ("done by a helper under this host"), never as something you did; Muse cannot check it.
- The tools server holds a device token for your home. It is kept in a file only you can read, but a host that runs commands as you can read your files too. Give a host you would not trust with your account neither door.

### What the host's token can and cannot do

The token `muse hosts setup` makes has the role `host`. The home checks the role on every call, before anything else, so this holds even if a bot takes the token out of its file and calls the home directly. It is enforced by the home, not left to the bot.

| A host token can | A host token cannot |
|---|---|
| Read status and the catalogue (what is left, what is connected, whether everything is paused) | Pause or resume |
| Ask you a question (create a card), and read the cards it asked for | Answer a card: not its own, not anyone's |
| Queue a job, and read or cancel the jobs it queued | See or cancel a job you or another device queued |
| Add a record of something it did | Read your records |
| List skills, run one, and read the runs it started | Give a skill a folder by path, a budget above the skill's own, or money to spend |
| | Store a key, start connecting a service, see or change apps, or make, change or remove device tokens |

Anything on the right is answered `403`; another device's job, card or run is answered `404`, the same as one that does not exist. `muse home token list` shows each device's role. A token made before roles existed (by an earlier `muse hosts setup`) has no role written down and is treated as `owner`, which can do everything: remove it and run setup again (`muse home token rm <name>-tools`, delete the token file, `muse hosts setup <name>`).

The role limits what the token can do **to the home**. It does not limit the bot's own shell and file tools, as said above.

### A permission for the host

`muse hosts setup <name> --permission` (or answering yes when it asks in a terminal) also gives the host's tools device a signed permission, holder `device:<name>-tools`, for 90 days:

| The permission says | What Muse does with it |
|---|---|
| Models through the Muse router, up to the caps of the host's app; no paid services | The host's app key is tied to the permission: it is refused (403, "permission stopped" or "has ended") from the moment you stop the permission or it ends. Stop it with `muse permissions stop <id>` or from the Permissions screen |
| Running skills, asked about each time | `check_permission` answers `ask` |
| No files of its own | `check_permission` answers `ask` for a path; skills reach your files only through folders you put on the skill list by name |

`check_permission` then answers from that permission (`basis: "permission"`, and `allow`, `ask` or `deny`). A host with no permission gets the interim rule, labelled `basis: "interim rule"` in every answer. Either way the answer is **advisory** for the host's own shell and file tools: Muse cannot block them. A host can also ask for a permission through the home (`POST /v1/mandates` from its device makes a card you approve); it can never issue or stop one itself.

## The tools

All names are fixed. Every result is a small JSON object; no result ever holds a key, a token or a prompt.

| Tool | What it does | State |
|---|---|---|
| `allowance_status` | What is left on each connected service, the next refill, today's paid spend against your cap, whether everything is paused. Estimates are marked | Built |
| `pause_status` | Whether you have paused Muse. Read-only: a bot cannot pause or resume | Built |
| `suggest_service` | The next service worth connecting and why. Only you can connect it | Built |
| `ask_approval` | Puts a card in front of you and waits: `approved`, `declined`, `expired`, or `pending` if the wait (at most 10 minutes) runs out | Built |
| `check_approval` | Looks up a card's answer without waiting | Built |
| `check_permission` | Whether an action may go ahead without asking. With a permission for this host it answers from that permission (`allow`, `ask` or `deny`); without one, an interim rule answers (`allow` only for reading something you listed with `--allow-read`, `ask` for everything else) and says so. Advice either way for the host's own tools | From the host's permission; interim rule without one |
| `record_action` | Adds a signed entry to your records saying what the bot did, and returns the entry's number and hash. It is the bot's own report: filed under `host.` (a bot's `files.write` is recorded as `host.files.write`), marked as done by a helper under this host, never as done by you, and Muse cannot check it. A summary is one line with no key or long code in it | Built |
| `queue_job` | Hands Muse a prompt to run later on free allowance. May use paid services only with an approved spend card | Built |
| `job_status` | How a queued job is doing. Never returns a prompt or an answer | Built |
| `list_skills` | The skills on your home, what each does, and the names of the folders a skill may be given | Built |
| `run_skill` | Runs one skill on your home and waits for its answer (at most 10 minutes; then `skill_status`). The skill reads only folders you put on a list, given by name (`muse skills folders add`); the bot cannot name a path. It never spends money through this tool. A file the skill proposes becomes a question to you, and Muse writes it only if you approve | Built |
| `skill_status` | How a run went, and what became of each file it proposed | Built |

Planned and deliberately absent:

- `pay`: planned. The payment part of Muse does not exist yet.
- `pause` and `resume`: never. A bot must not be able to resume itself. It gets `pause_status` only.
- Connecting a service, handling a key, creating or changing an app: never through these tools.

About approvals:

- A host may ask about four kinds of action: saving a file, sending a message in your name, spending, and sending something private to an outside service.
- The card says it was asked by a host bot. A host's token cannot answer any card; you answer from your own device or the Muse app.
- A host may have at most 20 questions waiting at once.
- If you choose "always allow", Muse records it but does not act on it yet. The bot is told to ask again next time.

## Hosts

| Host | Doors | Guide | How it was checked |
|---|---|---|---|
| OpenMausBot | Engine and tools | [openmausbot.md](openmausbot.md) | Configuration field names read on its own pages on 13 November 2026. Not installed, not run |
| Claude Code | Tools only (it has no need of the engine door; its model calls use your Anthropic sign-in, which Muse is not part of) | `muse hosts setup claude-code` prints the one command | Command read on its own documentation on 13 November 2026. Not run with Muse |

No other host has settings here. Goose, Hermes Agent and OpenClaw were not added because their current official pages for both doors were not read; nothing was guessed.

## Commands

```bash
muse hosts list                              # the hosts above, and how each was checked
muse hosts setup openmausbot                 # the host's own app key (shown once) and tools token (in a private file), plus what to paste
muse hosts setup openmausbot --permission    # the same, and a signed permission for the host that its app key stops with
muse hosts show openmausbot                  # print what to paste again; creates nothing, shows no secret
muse hosts check openmausbot ~/.openmausbot/config.json   # read-only: does an engine point at Muse; which engines do not
muse home serve                              # the tools server needs the home running
muse home token list                         # every device and its role (the host's is `host`)
muse skills folders add notes ~/Notes        # a folder skills may read when a host runs them; add --output to allow new files
python -m muse.mcp --token-file PATH         # what the host starts; also `muse mcp serve`
```

`muse hosts check` never changes the file and never prints a key it finds there. If the file is not in the form the host's documentation describes, it says "could not read this format" and stops.

The tools server needs the official `mcp` Python package, version 2.3.0 (`pip install -e '.[mcp]'` in this repository; the proxy's lock file already pins the same version). It speaks the Model Context Protocol over standard input and output, and logs to standard error only: which tool ran, how it ended, how long it took.

## Not verified

- No host was installed or run with Muse. Every step in the guides that touches a host is marked "not run here".
- The tools server was run only against the fake home (`python -m muse.home --fake`) and the SDK's own client, in tests.
- The local-socket form of the tools server in the spec is not built; only standard input and output.
