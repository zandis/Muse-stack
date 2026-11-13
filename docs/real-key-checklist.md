# Real-key checklist

Everything so far was tested against fake providers. This checklist runs a small, capped set of real calls with your own provider keys, on your own machine, so that two things can be confirmed:

1. The proxy, the rationing rules and the apps work end to end with real providers.
2. The 429 formats that `muse/quota.py` marks `# unconfirmed:` (Gemini, OpenRouter, Cohere) match what the providers really send.

Your keys never leave your machine. The script talks only to your local proxy, and the report it writes contains no keys, no prompts and no responses.

## Prerequisites

- Muse installed: `pip install -e .` (add `".[keyring]"` to keep keys in the OS keychain).
- At least one provider key set with `muse keys set NAME`. Free-tier keys are enough and are the point of the exercise.
- The proxy running: `muse up` (or `muse up --native`). Check with `muse status`.
- A terminal. The script asks you to confirm before it sends anything.

## Commands, in order

### 1. See what is configured

```bash
muse status
muse doctor
```

Fix anything `doctor` marks as failed before continuing.

### 2. The standard check

```bash
python scripts/real_key_check.py
```

It first prints exactly what it will send: for each configured provider, the most requests and tokens it can use, and the total. Every request is a few words with `max_tokens=16`. Type `y` to go on. (`--yes` skips the question.)

How to read the result:

- `✓` passed, `✗` failed, `·` a note, `-` skipped. Only `✗` makes the script exit 1.
- A reply with no visible text still passes when the model used its 16 output tokens on reasoning (gpt-oss and other reasoning models do). The note says so. The key and the route work; give such models a few hundred output tokens in real use. A model with `min_output_tokens` in the catalogue does not hit this: the router raises the 16 to the floor, the reply has text, and the check's detail says `output floor: max_tokens 16->256`.
- `HTTP 429 from the provider` is a note, not a failure: a shared free tier (OpenRouter `:free` models especially) was busy. The key was not rejected. Run the check again later.


It then does, in order:

| Check | What it proves |
| --- | --- |
| Proxy reachable | The proxy answers on its health endpoint. |
| One tiny call per configured provider (by deployment name) | The key works and the response carries `x-muse-*` headers. |
| Rate-limit headers per provider | Which limit headers the provider really returns. Gemini is expected to return none. |
| One tiny call per alias (`fast`, `smart`, `embed`, `rerank`...) | The router picks a working deployment for each alias. |
| Privacy `local` is refused when no local model exists | A "local only" request is never sent to a cloud provider. Costs nothing. |
| An app token works | A temporary interactive app (`rkc-int`) can call the proxy. |
| A background app is deferred right after an interactive call | The yield rule works with real providers. The temporary background app (`rkc-bg`) gets HTTP 429 `defer:yield` and nothing is sent upstream. |

The two temporary apps are removed when the script ends, even if it fails.

### 3. The paid-cap check (only if you have a paid key)

```bash
MUSE_DAILY_PAID_CAP_USD=0 muse restart
python scripts/real_key_check.py --expect-paid-cap-zero
muse restart            # back to your normal cap
```

With the cap at 0, every paid deployment must refuse. The script calls each configured paid deployment once and reports a failure if any of them answers. A refused call costs nothing. If one did answer, the cap is not working: stop and send the report.

### 4. The 429 probe (optional, uses quota)

This is how the unconfirmed 429 parsers get confirmed. It deliberately triggers a rate limit, so it uses up real quota and can leave a free tier unusable for the rest of the day (or month, for trial keys). Run it once per provider you want checked, on a day you do not need that provider:

```bash
python scripts/real_key_check.py --probe-limits --provider gemini --burst 25
```

Pick the provider with `--provider` (a provider id from `muse status`; Gemini, OpenRouter and Cohere are the ones that matter). The script sends up to `--burst` requests (default 25, at most 60) until the provider answers 429, then stops. It records the status, the header names and numeric values, the shape of the error body with all text replaced by its length, which parser keywords the text contains (for example `per day`, `quota`), the "try again in" duration, and what Muse decided to do about it (blocked for how long). It never records the message text.

## What each result means

- `✓` the check passed.
- `✗` the check failed. The detail says what to look at. A provider call that fails with HTTP 401/403 means that key is wrong; fix it with `muse keys set NAME`, then `muse restart`.
- `-` skipped, with the reason (for example, a local model exists, so the "no local model" check does not apply).
- `·` information, such as the rate-limit headers a provider returned.

If `background-yield` fails with HTTP 200, check that `MUSE_ACTIVE_WINDOW_S` is not set to 0 in your environment.

## What to send back

Send the file `~/.muse/real-key-report.json` (or the path you gave with `--report`). Open it first if you like: it is plain JSON and holds only names, numbers and status codes. It contains no keys, prompts or responses. If you find any of those in it, that is a bug: do not send it, and tell us.

## After the run

- Temporary apps `rkc-int` and `rkc-bg` are already gone. `muse apps list` should not show them.
- Nothing in this check needs rotating. If you want to be extra careful after sharing logs or screenshots from this session, rotate the provider keys at each provider's console and update them with `muse keys set NAME`, then `muse restart`.
- To rotate an app key you created: `muse apps rotate NAME`. To revoke one: `muse apps rm NAME`.
- To revoke a provider key, delete it in the provider's console, then `muse keys rm NAME`.

## Expected cost

On free tiers: zero. Each request is about 50 tokens or fewer in total.

What could cost money, and what stops it:

| Risk | Why it should not happen | Protection |
| --- | --- | --- |
| A paid provider answers a check call | Paid deployments are only used by apps allowed to, and only inside the daily cap | `MUSE_DAILY_PAID_CAP_USD` (default 1.00; 0 turns paid use off). The temporary apps are not allowed to use paid providers at all. |
| The owner key calls a paid deployment by name | Step 2 calls each configured provider once by deployment name, using the owner key, which is allowed to pay | Cost is a few tokens (well under a cent) and counts against the daily cap. Remove the paid key first if you want zero risk. |
| A trial key runs out | Cohere trial keys have 1,000 calls a month | The check uses about 5 calls on it. |
| The 429 probe | It spends free quota on purpose | It stops at the first 429 and never sends more than `--burst` requests. It does not spend money on a free-tier key. |

The maximum number of requests and tokens is printed before anything is sent, per provider and in total, and you can stop at that prompt.
