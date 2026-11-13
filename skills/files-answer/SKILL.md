---
name: files-answer
description: Answers a question from the person's own files, citing the passage behind every statement. Use it when the answer should come from documents in folders the person names, not from general knowledge.
license: See the repository licence
metadata:
  author: Muse
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: everyday
  privacy_default: standard
  budget:
    requests: 12
    tokens: 60000
    seconds: 180
    paid_usd: 0
  needs:
    files.read: [chosen]
    files.write: [data]
    net: []
  writes: false
  check: check
---
# Answer over my files

Ask a question; get an answer taken only from the files in the folders you name, with each statement pointing at the file and paragraph it came from.

How it works:

1. It reads the text files in the folders you name (text, Markdown, CSV, JSON, HTML; PDF only when a PDF reader is installed) and keeps a search index of their text in Muse's own folder on this computer. Unchanged files are not read again.
2. It finds the few passages most related to your question on this computer. Only those passages and your question are sent to a model, through Muse's router, under the privacy class of the run. The default is standard, the owner's chosen default: free services may learn from what is sent. Run with `--privacy private` to send only to services that do not, or `--privacy local` to keep everything on this computer.
3. The model answers from those passages only and cites them like [2].
4. Before you see it, Muse checks that every statement cites a passage that exists and shares words with it. An answer that fails is tried once more on a stronger model; if that fails too, you are told, and the answer is marked as not checked.

What it cannot do: read scanned images, follow links, or answer from files outside the folders you named. When the passages do not hold the answer it says so instead of guessing.

Inputs: `question` (required); `top_k` (how many passages to send, default 6, at most 12); `embeddings` (`auto` or `off`).
