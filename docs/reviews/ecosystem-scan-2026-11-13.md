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

The advisory details above come from the scan and have not been re-read by a second pass. Each is to be confirmed on the project's own advisory page before acting.

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
