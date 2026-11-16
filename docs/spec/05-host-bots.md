# Muse v1 specification, part 5: running under an existing bot

Written 2026-11-13, after the owner asked whether the helper should be an existing bot or bot host, naming OpenMausBot as an example. This part answers that and amends parts 1 and 4.

Evidence marks: **[V]** read on the project's own pages on 2026-11-13 (repository page, README, LICENSE, SECURITY.md, self-hosting and custom-engine docs); **[U]** not verified. The project was read, not installed or run.

## 1. What OpenMausBot is

| Point | Finding |
|---|---|
| What | A desktop "team of agents" chat app with a local server that owns the agent processes [V] |
| Licence | Apache-2.0, except an `enterprise/` folder with its own licence [V] |
| Stack | TypeScript, React, Electron; Node 24+; desktop installers, npm package, Docker, a one-command VPS installer [V] |
| Activity | About 21.8k stars, 710 open issues, release v0.1.154 dated 12 November [V for the page; the year and the contributor count are not shown] |
| Models | Drives the Claude, Codex and Grok command-line tools through the user's own logins; also accepts any OpenAI-compatible address with a key and model name [V] |
| Actions | Shell and file edits show Allow / Deny cards; a per-bot "Full access" switch bypasses them [V] |
| Schedules | Timed, webhook and app-event triggers, while the app or server is running [V] |
| Devices | Pairing with a 12-character code; a phone app; a companion mode over Tailscale [V] |
| Connected apps | Through Composio, a third-party cloud service [V] |
| Chat platforms | LINE, Telegram, WhatsApp and the like are not listed as channels [V] |
| Security notes | Agents run with the user's own privileges. Its security file describes a known, unfixed way for a bot's shell on a shared or hosted server to post into a full-access thread. The "safety floor" is off by default. Keys in its config file are plain text at mode 600 [V] |
| Data | Transcripts and keys under the user's home folder; encryption at rest is not stated [V] |
| Money | "Has no token" [V] |

## 2. How it compares with what Muse specifies

| Muse module (part 1) | Does OpenMausBot already have it? |
|---|---|
| Helper runtime: jobs, steps, ask before writing | **Yes**, mature |
| Schedules | **Yes** |
| Desktop app, phone app, pairing | **Yes**, for a technical audience |
| Tools through MCP | **Yes** |
| Router that rations free allowances | No. It can use Muse's router as an OpenAI-compatible engine |
| Catalogue of free allowances, guided sign-up | No |
| Sandbox for tools | No: agents run as the user |
| Vault with records only readable on the person's devices | No |
| Permissions that narrow per skill and stop everywhere at once | No: Allow / Deny per action, plus a bypass switch |
| Signed records | No |
| Spending limit, payments, credits | No |
| LINE | No |
| Plain-word screens in Traditional Chinese for non-technical people | No |

So it covers the bot and its shell well, and none of what makes Muse different.

## 3. Recommendation

**Use an existing bot as the helper wherever one fits, and make Muse the layer underneath any bot. Do not fork one as the product.**

Why not fork it:

- **Audience.** It is built for people who already use coding tools. Muse's first users do not know what a key is.
- **Trust model.** Agents run with the user's full privileges and there is a bypass switch. Muse's promise is that the helper cannot exceed what was approved. Retrofitting that into a fast-moving code base with 700 open issues is more work than building the thin runner Muse needs.
- **Zero trust.** Connected apps go through a third-party cloud that holds the tokens. That contradicts "only your devices can read it".
- **Dependency risk.** Version 0.1, one named author, a separate licence for part of the tree, a known unfixed security issue.
- **Subscription logins.** ⚖ Its main engines drive vendors' command-line tools under the user's consumer login. Muse's rule is that subscriptions are hand-off only. Muse must connect to such a host only through the OpenAI-compatible engine setting, never through those logins.

Why use it, and others like it, as a host:

- It already does the hardest-to-polish part: the agent loop, schedules, approvals, a phone companion.
- The owner's original aim was infrastructure. A router and permission layer that works under several bots reaches more people than one more bot.
- It lets real chores run on Muse's rationing now, months before Muse's own app exists. That is exactly what the first gate needs.

## 4. What changes in the design

Muse gains a **host adapter**: two doors any bot can use.

| Door | What the host gets | State |
|---|---|---|
| **Engine** | Muse's OpenAI-compatible endpoint with an app token per host: rationing, privacy classes, the paid cap, the three choices in the refusal text | Exists. Needs a preset and a setup guide per host |
| **Tools (an MCP server Muse ships)** | `allowance_status`, `suggest_service`, `ask_approval` (creates a Muse card and waits), `check_permission`, `record_action`, `pay` (test network), `run_skill`, `queue_job` | New: work package H2 |

With the adapter, a person can choose:

| Helper | For whom | What Muse enforces |
|---|---|---|
| **Muse's own thin helper** (the skill runner plus the app in part 2) | Non-technical people; the default | Everything in part 1 |
| **A host bot such as OpenMausBot** | Technical early users; the owner's own test bench | Model use, spending, anything done through Muse's tools. **Not** what the host does with its own shell and file tools |

That last line must be said plainly in the app and the docs. Under a host bot, Muse's permissions cover Muse's doors only. The records will show what went through Muse and nothing else.

The tools door is itself limited (built 13 November 2026). The tools server's device token has the role `host`, and the home control API checks a role on every call (part 1 §4.1): a host token can read status and the catalogue, ask the person a question and read its own cards, queue, read and cancel its own jobs, add records, and list and run skills. It cannot pause or resume, store keys, connect services, manage apps, answer a card, read the records, or see another device's jobs, cards or runs. The tools built so far are `allowance_status`, `pause_status`, `suggest_service`, `ask_approval`, `check_approval`, `check_permission` (advisory), `record_action`, `queue_job`, `job_status`, `list_skills`, `run_skill` and `skill_status`; `pay` waits for the payment package.

Amendments to earlier parts:

- **Part 1 §2.3 (helper runtime).** Stays thin on purpose: a runner for Muse's own skills, schedules and cards. It does not grow into a general agent framework. General agent work is delegated to a host bot through the adapter when the person has one.
- **Part 1 §2.11 (connectors).** Each connector is already an MCP server; the same servers are offered to host bots.
- **Part 4, new packages.**

  | ID | Package | Size | Needs | Acceptance |
  |---|---|---|---|---|
  | H1 | Host presets and setup guides: OpenMausBot first, as an OpenAI-compatible engine with its own app token and priority; a check that the host is not using a subscription login for Muse's traffic | 2 | — | A person follows one page and the host's answers show Muse's route headers in the records |
  | H2 | Muse MCP server (stdio and local socket) exposing the tools above over the home control API | 5 | A2, A3 | A host bot asks for approval and the card appears in Muse; a declined card stops the action |
  | H3 | Run the four first chores through a host bot on real free allowances, on the owner's VM | 2 | H1, A9 | Feeds gate G1 with data months before Muse's own app |

  H1 and H3 are on the path to G1. A11 (helper runtime) shrinks from 6 days to about 3.

## 5. Not verified

- OpenMausBot was not installed or run. How well it works with a non-vendor OpenAI-compatible engine, and whether its schedules run headless on a small VM, are untested.
- Its contributor count, the year of its latest release, and the terms of its `enterprise/` licence.
- Whether vendors' terms allow a third-party app to drive their command-line tools under a consumer login ⚖. Muse does not depend on the answer, because it does not use that path.
- Other hosts worth a preset (OpenClaw, Hermes Agent, Goose) were reviewed in `docs/ecosystem-review-2026-11.md` but not tested either.
