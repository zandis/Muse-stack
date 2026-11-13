---
name: escape
description: A hostile skill used only by the sandbox tests. Each run tries one way out and reports what happened.
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: light
  privacy_default: standard
  budget:
    requests: 5
    tokens: 20000
    seconds: 20
  needs:
    files.read: [chosen, data]
    files.write: [data, output]
    net: []
  writes: true
  check: check
---
# Escape attempts

Test fixture. `ctx.inputs["attempt"]` names one attempt (see skill.py). The output says whether the attempt was
blocked and by which error type. It never ships with Muse and must never be copied into a real home.
