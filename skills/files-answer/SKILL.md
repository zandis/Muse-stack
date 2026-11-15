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

1. It reads the text files in the folders you name (text, Markdown, CSV, JSON, HTML, and PDFs that have a text layer when Muse's optional PDF reader is installed) and keeps a search index of their text in Muse's own folder on this computer. Unchanged files are not read again. A PDF is cited by page: `lease.pdf › page 3, paragraph 2`. A scanned PDF (no text layer), a PDF locked with a password and a damaged PDF are not read, and the notes name each one; without the PDF reader the notes say how many PDFs were skipped.
2. It finds the few passages most related to your question on this computer. Only those passages and your question are sent to a model, through Muse's router, under the privacy class of the run. The default is standard, the owner's chosen default: free services may learn from what is sent. Run with `--privacy private` to send only to services that do not, or `--privacy local` to keep everything on this computer.
3. The model answers from those passages only. Every claim is an exact quote from one passage plus what it says in your language, and the answer cites claims like [2].
4. Before you see it, Muse checks every claim on this computer: the quote must really be in the passage it names (small differences in spaces, punctuation and full-width characters are allowed, a different digit never is), every number the claim states must be in its quote, and every sentence of the answer must cite a claim and hold only numbers its quotes hold. When the question names something (Flat 4B, INV-3109), the answer must come from a file that mentions it, and a question that asks for two things needs a claim for each or must say what was not found. Claims that fail are dropped; an answer with none left is tried once more on a stronger model, and if that fails too you are told and the answer is marked as not checked. The Sources list shows each quote, so you can see the exact words.
5. A model that returns no text at all (a "reasoning" model that spent its allowance thinking) is asked once more with more room; if it is still empty you are told the question was not answered, never that your files do not hold the answer.

What it cannot do: read scanned images or scanned PDFs (there is no OCR), open a PDF that needs a password, follow links, or answer from files outside the folders you named. When the passages do not hold the answer it says so instead of guessing.

Inputs: `question` (required); `top_k` (how many passages to send, default 6, at most 12); `embeddings` (`auto` or `off`).
