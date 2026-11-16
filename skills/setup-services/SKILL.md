---
name: setup-services
description: Says which services are connected and which ONE to add next, with the time it takes, what it adds, its privacy badge and the exact steps from Muse's catalogue. Use it when the person asks what to connect, or how to get more free allowance.
license: See the repository licence
metadata:
  author: Muse
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: light
  privacy_default: standard
  budget:
    requests: 1
    tokens: 2000
    seconds: 30
    paid_usd: 0
  needs:
    files.read: []
    files.write: []
    net: []
  writes: false
  check: check
---
# Set up my services

Tells you what is connected now and the one service worth adding next: what it adds, how many minutes it takes, whether it needs a card or a phone number (or "not confirmed"), its privacy badge, and the steps to follow, one at a time, in English or Traditional Chinese. Up to two alternatives follow, and the honest caveats (for example, that a free tier may learn from what you send).

How it works:

1. It reads Muse's catalogue of services and checks which ones already have a key on this home. It only asks whether a key is present; it never reads one.
2. It ranks what is left the way `muse connect` does: one tap before pasting a key, no card or phone before "maybe", then the most allowance gained per minute of setup. Paid-only services and "AI on this computer" are left out unless you ask for them.
3. If you give a goal ("long documents", "private", "faster", 「長篇文件」, 「隱私」), services that fit it move to the front. The matching is a small word list, not a model.
4. Only when your goal matches no word in that list, one small model call may phrase a one-line reason. The order does not depend on it, and the skill works with no model at all.
5. Before you see it, Muse checks that every recommended service is in the catalogue and not yet connected, that every number shown is in that service's catalogue entry, that the steps are there in your language (or in English, with a note), and that nothing shaped like a key appears.

What it never does: open a browser, sign up for you, type into a service's website, or store a key. It returns the steps and the command to run (`muse connect <id>`).

Inputs: `goal` (optional free text); `lang` (`en` or `zh-TW`; default: the language of the goal, else MUSE_LANG); `include` (`paid`, `local` or both, to let those be suggested); `phrase` (`auto` or `off`: whether the one model call may be used).
