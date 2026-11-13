# Scan of trending projects, 2026-11-13

A check of GitHub's trending pages and of the categories that match this project's open problems. The scan was done by an assistant reading project pages; star counts and some licences were read from summaries, not from the files themselves. Nothing here has been installed or run. Marks: [V] read on the project's own page or package index, [S] secondary, [U] unverified.

What is trending overall is mostly agent-skill packs and coding-agent shells. Few of those matter here; most of the list comes from targeted checks.

## Worth acting on

| Item | What it gives this project | Licence | Verdict |
|---|---|---|---|
| **liteparse** (run-llama/liteparse) | Local PDF and Office to text with page and position data; Rust core with Python bindings; about 0.007 s a page with OCR off in one person's benchmark [V page, single author] | Apache-2.0 per README and package index [V]; LICENSE file not read | **Adopt** for PDFs that have a text layer. Closes the files skill's biggest gap: it skips PDFs today |
| **sandbox-runtime** (anthropics/sandbox-runtime) | A design for letting sandboxed code reach only allowed web hosts: no network inside the sandbox, a bridge to a host-side proxy with an allow-list, and a block on private addresses after name resolution [V] | Apache-2.0 [V]; labelled a beta research preview | **Copy the design**, not the package. It is what the skill sandbox needs before any skill may fetch a page |
| **Reasoning control in LiteLLM** | The docs say `reasoning_effort` accepts `none` and `minimal` [V docs]. Not a new project, but the likeliest fix for the 22% empty answers measured on 13 November | — | **Verify per provider, then use** for short tasks. Passed to the research-skill fix |
| **pytest-xdist** | Run the 15-minute test suite in parallel; tests that start servers can be pinned to one worker [V] | MIT [U] | **Try** |

## Worth a small trial

| Item | For | Note |
|---|---|---|
| potion-multilingual-128M (static embeddings, MIT) and Qwen3-Embedding-0.6B (Apache-2.0) [V] | Finding passages across Traditional Chinese and English | Test on our own questions first. The files skill already scored 6 of 6 across languages on real runs, so the need is unproven |
| Gemma 4 small instruction models (Apache-2.0) [V] | A local fallback when free allowances run out, and for content that must stay on the device | Traditional Chinese quality and speed on a small ARM machine are unknown [U] |
| RapidOCR (Apache-2.0) [V] | Scanned documents | Traditional Chinese model support is not stated [U] |
| agent-browser (vercel-labs, Apache-2.0) [V] | A future "fetch a page" tool behind the allow-list proxy | Has a domain allow-list and action policies |
| SkillSpector (NVIDIA, Apache-2.0) [V] | Scanning a skill before it is approved | Run with its model stage off |

## Watch

- **freellmapi** (tashfeenahmed/freellmapi, MIT): the closest competitor, and growing fast. One endpoint over many free providers, with key rotation and per-key counters. Its catalogue is served from its own site, delayed 30 days unless paid. Not seen there: privacy classes, a daily spending limit, per-app priorities, a non-technical app. Those are this project's difference. Its key rotation across accounts is something this project refuses to do.
- **agentgateway** (Linux Foundation, Apache-2.0): a proxy for tools and models with rule-based access control. Compare its policy model with ours; too large to embed.
- **OpenShell** (NVIDIA, Apache-2.0): credentials injected only for approved hosts, and risky policy changes held for a person. Ideas to copy.

## Avoid

- **CLIProxyAPI**: turns personal subscription logins into an API. Against provider terms; this project's rule is hand-off only.
- **Lightpanda** and **pymupdf4llm**: AGPL.
- **chrome-devtools-mcp**: sends usage statistics by default, no domain restriction. **Scrapling**: advertises bypassing site protections.

## Security items to check in what we already use

| Dependency | Report | Our exposure | Action |
|---|---|---|---|
| LiteLLM 1.108.5 (pinned) | An advisory published 12 November: an authenticated user can extract secrets through Azure credential parameters; fixed in 1.108.6 [V per the scan] | The catalogue has no Azure entries and the proxy listens on localhost only, so exposure is low | Upgrade to 1.108.6 deliberately: new digest, regenerated lock, full suite |
| MCP Python SDK 2.3.0 (pinned) | Six advisories between 28 September and 5 October [V per the scan] | The tools server uses it over stdio only | Confirm 2.3.0 contains the fixes |
| Tauri | An advisory on 26 September about the fetch command's access control [V per the scan] | The app does not use the HTTP plugin | None, unless that plugin is added |
| iroh | 1.4.0 fixed TLS-library advisories [V per the scan] | Pairing prototype only | Pin 1.4.1 or later |

The advisory details above came from the scan. They were then checked on each project's own pages on 13 November:

| Dependency | What was confirmed | Done |
|---|---|---|
| LiteLLM | [V GitHub advisory GHSA-2rwg-2wh4-7c4f, BerriAI/litellm] Published 12 November, high. Six Azure Entra fields in a request body could override a deployment's credential and echo server secrets in an error. It needs an `azure/` or `azure_ai/` model. Fixed in 1.108.6 (also 1.106.9 and 1.109.1). [V PyPI JSON] 1.108.6 exists, uploaded 12 November 01:07 UTC, not yanked. [V ghcr.io registry] `v1.108.6` resolves to index `sha256:c64fe10f…3ec3`, signed with the key LiteLLM rotated to in October (signature checked by hand). [V release notes v1.108.6] The six fields now go in `metadata`. | Upgraded to 1.108.6: image tag and digest, both lock files regenerated, docs and tests. The LiteLLM files the router relies on are byte-identical to 1.108.5. Hook, integration and slow tests pass. |
| MCP Python SDK | [V GitHub advisories, modelcontextprotocol/python-sdk] Six advisories published 28 September to 5 October. Five are fixed in 2.2.0 and one (request body size) in 2.1.0. [V PyPI JSON] 2.3.0, uploaded 2 October, is the latest. | No change: 2.3.0 already has all six fixes. |
| Tauri | [V GitHub advisory GHSA-w28w-mhc8-qvjv, tauri-apps/tauri] Published 26 September, high. The scan was wrong about its scope: it is Tauri's own internal channel fetch command, not the HTTP plugin. Affected 2.0.0 to 2.11.5, fixed in 2.11.6. The app pins `tauri` 2.12.5. | No change: the pin is past the fix. |
| iroh | [V GitHub release notes v1.4.0, n0-computer/iroh] 1.4.0 updates rustls for four RUSTSEC advisories. [V GitHub advisories, rustls/rustls] Those are fixed in rustls 0.23.46; `core/link` locks rustls 0.23.46. [V GitHub advisories, n0-computer/iroh] iroh's own three advisories (5 October) are fixed in 1.1.0. `core/link` pins iroh 1.4.1. | No change: both pins are at or past the fixes. |

## The Handoff² article

The article at hermes-ai.net is a rewrite of a vendor post. Handoff² is a closed feature of a paid consumer app (Hark): no code, model, API or licence. Its headline "beats Claude Opus" contradicts its own text, which says "within five points" of Opus and about twice as fast. All figures are the vendor's, on an unpublished benchmark. One idea is worth testing here: fewer, larger model calls. On 13 November the files skill made 2.2 calls a run and was right 97% of the time; the research skill made 11.7 and was right 41%.

## DeepSeek's chat demo (deepseek-ai/chat-demo)

Read on 2026-11-13: the repository page, README, licence, package file and its API client source. Not installed or run. It sits under the organisation that links to deepseek.com; MIT; two commits; no tests.

What it is: a static chat page with no server of its own. The browser talks straight to DeepSeek's API with a key the person pastes into Settings, kept in the browser's storage.

| What it does | What this project takes from it |
|---|---|
| Each request can switch the model's hidden reasoning on or off (`thinking: enabled / disabled`) | Confirms the direction of the research-skill fix: short tasks should ask for no reasoning where a provider allows it. DeepSeek is a paid entry in the catalogue, so this is a note for that entry, to verify on DeepSeek's own API docs |
| A reply that stops at its length limit can be continued | **Adopt**: a "Continue" action in Ask when an answer is cut short or comes back empty. Passed to the app work |
| Files are uploaded to the provider, which parses PDFs and Office files and cites them | **Do not copy as the default.** The file leaves the device. Muse reads files locally first; a provider's parser could be an option for scans, shown with the "Standard" badge |
| When the provider forgets an uploaded file, the app re-uploads from the copy it kept | The pattern to keep: originals stay local, anything remote is disposable |
| Web search and page reading are the provider's own tools, with citations returned | **An idea to test**: answer web questions by asking a provider's built-in search tool, instead of Muse fetching pages. It needs no fetching sandbox. The question leaves the device, so it is "Standard" only. Which free services offer such a tool is unverified |
| The README says deleting chats does not delete files already sent to the service | **Adopt** the honesty: say in the app what stays with a service after local deletion |
| Markdown, tables and code are rendered from a parsed tree | **Adopt** for Ask, without ever inserting HTML |
| Message branching, edit and regenerate, automatic titles, voice input and read-aloud | Later. Voice matters for non-technical users; it is outside the first release |
| The key is pasted and stored in the browser's storage; Simplified Chinese and English only | Where Muse differs: the app's web view never holds a key, the first sign-in needs no pasting, and Traditional Chinese comes first |

## How AWS's agents built its emulator

Read on 2026-11-13: the article on aboutamazon.com (11 November 2026, by a senior principal engineer; the first of a series) and the READMEs of `aws/aws-emulator` and `aws/aws-emulator-evaluation-suite`. Not installed or run. The emulator itself is of no use here, since this project does not use AWS. The method is.

What they did, as stated:

- **A specification and a reference.** The specification is AWS's published API models; the reference is real AWS.
- **Tests are checked against the real thing first.** Every fidelity test was verified against at least two real AWS accounts before it counted. The tests can then be replayed without calling AWS.
- **Three bounded roles.** Test writers; issue finders, who replay the tests and file failures; fixers, one agent per issue.
- **A merge gate.** Build, review against coding standards, security, and no drop in fidelity. A change that fails goes back.
- **Known gaps are files in the repository**: "things that do not work" and "things that now work".
- **A lesson they report**: reuse existing libraries. An early version wrote its own Python interpreter.

Caveats: the article gives no fidelity figures; the team says nobody wrote or reviewed the code; the emulator is stated to be unfit for production or security testing. That is acceptable for a test tool and would not be for this project's vault, permissions or pairing.

What this project takes from it:

| Their practice | Here |
|---|---|
| Tests verified against the real system, then replayed offline | The weakest point today: every test uses scripted replies, and real runs twice found what they missed. **Record real provider replies on the VM (shapes, headers, error bodies, timings; never content) and replay those in the test upstream**, so offline tests stand on observed behaviour |
| A fixed set of fidelity tests as the unambiguous goal | The 133 expected-fact tasks play this role for skills. Gate 1 is now judged on them |
| No change merges if fidelity drops | Add to the merge rule: the nightly real-run score on a fixed sample must not fall |
| Known gaps tracked as files | Started: `docs/known-gaps.md`, seeded from the real runs |
| Small bounded tasks for agents, one issue each | Matches how work packages are run here; the lesson is to keep them small and tied to a failing check |
| Humans do not review | **Not adopted** for security code, which stays unwired until an outside review |
