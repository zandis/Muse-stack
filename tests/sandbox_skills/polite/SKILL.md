---
name: polite
description: A well-behaved skill used only by the sandbox tests. It uses every part of the context a skill may use.
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: light
  privacy_default: standard
  budget:
    requests: 5
    tokens: 20000
    seconds: 30
  needs:
    files.read: [chosen, data]
    files.write: [data, output]
    net: []
  writes: true
  check: check
---
# Polite

Test fixture. Reads the folders it was given, keeps a file in its data folder, asks the model, proposes a file.
