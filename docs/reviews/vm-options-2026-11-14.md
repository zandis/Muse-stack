# What the VM and compute review of 14 November means here

Source: a review page shared by the owner ("VM & compute options review 2026-11", dated 14 November 2026, Asia/Taipei). It says its figures were read from providers' official pages and that nothing was signed up for. The figures below are that review's; none was re-read for this note. It covers other projects of the owner's as well; only what touches this repository is taken up here.

## What it says that matters to this project

| Point in the review | Effect here |
|---|---|
| Oracle's Always Free ARM allowance is 2 cores and 12 GB since June 2026. Paid accounts are reported to keep 4 cores and 24 GB free | No change to the design: the one-button VM setup already refuses more than 2 cores and plans for 12 GB. The test VM is larger than that, so its account type must be checked (VM test plan, step 0.1) |
| An Always Free instance may be stopped after 7 days with processor, network and memory all under 20% | The idle guard exists for this. Decided on 15 November 2026: on by default for Always Free accounts, off for paid ones (recommendation 2) |
| The VM is in San Jose; no real patient data on it, on any free tier, or on a low-cost host | Already a standing rule of this project. Health data stays a parked add-on |
| A paid stand-by in Singapore costs about US$9 to 15 a month (Hetzner, OVH) | The catalogue's compute entries should list these as paid options with their traffic limits, after reading each provider's page. A stand-by also makes "your helper moved" a case the app must handle: backups restore to a new machine |
| Free hosts that sleep or expire (Render, Railway's free plan, Google's small free VM) are unfit for an always-on helper | Matches the catalogue's rule that an entry states whether it can run all month |
| Startup credit programmes (Cloudflare US$10k with no funding minimum, Oracle, Microsoft, AWS, Google) | An owner decision, outside the code. Credits never change the rules: one account per provider, official APIs only |
| Free GPU notebooks (Kaggle 30 hours a week, Colab) and hourly GPUs | Not needed for the first release. Relevant later to the local-model fallback trial |

## Recommendations

1. **Do the account check before anything else on the VM** (step 0.1). If the account is Always Free and the instance is over 2 cores and 12 GB, the gate re-run could lose its machine mid-run.
2. **Turn the idle guard on by default for Always Free accounts, off for paid ones.** The rule needs all three measures low, so holding the processor's 95th percentile near 25% is enough. The guard's cost is a few watts of real work on a machine that is otherwise free; the alternative is a helper that disappears after a quiet week. **Decided (15 November 2026), and implemented:** the stack's `account_type` (default `always_free`) sets `idle_guard`'s default (`auto`); `on` and `off` still override. The guard uses the processor only, one minute in ten unless real work already reached 25%, backs off whenever real work runs, never holds memory or sends traffic, and logs one line a day. The rule itself was re-read on Oracle's Always Free page on 15 November 2026 and matches the review.
3. **Keep the first-release target at 2 cores and 12 GB.** It is the only size every new Oracle account can have for nothing, and the test VM being larger hides problems a tester would meet. Stage 2 of the VM plan should be run with the services limited to that size.
4. **Add an outside check that the helper is up.** A stopped instance is silent. The app already shows "helper not reachable"; a free external probe or a daily message from the helper covers the case where nobody opens the app.
5. **Do not treat a second provider as a second free allowance for the same person in the same service.** A paid stand-by on another provider is fine; a second free account on the same provider is not.

## Not verified

The June 2026 change date, the paid-account allowance, every price, and programme terms. Each is to be read on the provider's own page before money or an application depends on it.
