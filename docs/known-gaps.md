# Known gaps

A running list of things that are known not to work, each tied to evidence that can be re-run. A gap leaves this list only when the same evidence shows it fixed. The idea is borrowed from how AWS's agent-built emulator tracks "things that do not work" (see `docs/reviews/ecosystem-scan-2026-11-13.md`).

## From real runs

Source: `docs/reviews/data/gate1-full-2026-11-13.csv` (133 runs on real providers, 13 November 2026). "Wrong" means wrong by expected facts, whatever the skill's own check said.

### Answer over my files: 2 wrong of 67

| Task | Kind | Facts found | What went wrong |
|---|---|---|---|
| `fa-027` | multi-hop | 1 of 2 | Answered one part of a two-part question |
| `fa-035` | not-found | 0 of 0 | Answered when the files do not say |

Also open: 8 of 67 runs took 20 to 87 seconds; four of those had no recorded wait on a rate limit and are unexplained.

### Research report: 39 wrong of 66

| Kind | Wrong | Tasks |
|---|---|---|
| cross-lang | 5 of 5 | `rr-062`, `rr-063`, `rr-064`, `rr-065`, `rr-066` |
| numeric | 5 of 6 | `rr-016`, `rr-017`, `rr-018`, `rr-047`, `rr-049` |
| multi-hop | 8 of 10 | `rr-022`, `rr-023`, `rr-024`, `rr-025`, `rr-026`, `rr-027`, `rr-052`, `rr-054` |
| long-file | 7 of 9 | `rr-028`, `rr-029`, `rr-030`, `rr-056`, `rr-057`, `rr-058`, `rr-059` |
| lookup | 13 of 26 | `rr-001`, `rr-003`, `rr-005`, `rr-008`, `rr-009`, `rr-010`, `rr-011`, `rr-012`, `rr-015`, `rr-036`, `rr-037`, `rr-038`, `rr-040` |
| date | 1 of 5 | `rr-019` |

Measured causes: 172 of 774 calls came back empty (a reasoning model used its whole output on hidden reasoning); the skill's own check passed 30 wrong reports. A fix for both is in progress. Re-run the tasks above to confirm.

## From the first automated run on the work branch (13 November 2026)

Fixed the same day, kept here as a record of what local testing missed:

- A test label depended on the Rust compiler version.
- The home and dashboard servers answered a badly broken request with no status line on Python 3.12.
- Deployment scripts had warnings from a checker that was not installed locally.

## Router classification (13 November 2026)

From the review on PR #8 (`docs/reviews/2026-11-13-dsh-our-free-model-ideas.md`). The checks are in `tests/test_quota.py`, `tests/test_router_fidelity.py`, `tests/test_hook_unit.py` and `tests/integration/test_fakeprov_router.py`. How to capture a real response and replay it is in `docs/testing/fidelity-and-replay.md`.

| Gap | Where it stands |
|---|---|
| A 429 whose text says "quota" or "limit" and names no clock | Fixed. The upstream is exhausted until the estimated daily reset (midnight Pacific for Gemini, UTC midnight otherwise). A bare 429, with neither word, stays a 60 second cooldown. |
| Gemini PerDay blocks only the model that returned it | Open. The catalogue says the limit is per project and per model. The block stays on that one upstream until a cassette shows the cap is project-wide. The desired test is `xfail(strict=True)`. |
| An error frame inside an HTTP 200 SSE stream | Open. The failure hook is the only place that classifies, and a 200 never arrives there, so the upstream stays eligible and the fallback is not taken. The desired test is `xfail(strict=True)`. |

A 403 whose message says the quota is used up is still the bad-key rule: the whole provider, for 15 minutes. Empty visible text from a reasoning model with a small `max_tokens` is the output floor in `muse/hook.py` (1,024 on the gpt-oss deployments); the fake provider covers that shape.

## Never verified against the real thing

Each of these is tested only against a stand-in. They are gaps until a real run says otherwise; `docs/vm-test-plan.md` says how.

| Area | What is unverified |
|---|---|
| Rate-limit replies | The exact error formats for Gemini, OpenRouter and Cohere (marked `unconfirmed` in `muse/quota.py`) |
| Output floor for reasoning models | Whether 256 or 1,024 tokens is enough on each provider; behaviour when replies are streamed |
| Sandbox | Isolation on ARM; macOS is written but untested; Windows has none |
| Locked install | Whether every pinned package has an ARM build |
| One-button VM setup | Never applied to a real Oracle account; the Terraform was never validated by Terraform |
| Host adapter | Never run with a real host bot |
| One-tap sign-in | Never run against the real OpenRouter |
| Desktop app | Tray icon and panel never seen; no macOS or Windows build; no installer |
| Vault, permission tokens, pairing, sandbox | No outside security review |
| Traditional Chinese | App wording and the evaluation set have not been read by a native Taiwan reader |
