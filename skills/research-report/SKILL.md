---
name: research-report
description: Writes a short report that answers a question from the person's own documents, with a source behind every statement. Use it when the answer needs several facts gathered from more than one place in the folders the person names.
license: See the repository licence
metadata:
  author: Muse
muse:
  version: "0.1.0"
  entry: skill.py:run
  tier_default: light
  privacy_default: standard
  budget:
    requests: 40
    tokens: 150000
    seconds: 900
    paid_usd: 0
  needs:
    files.read: [chosen]
    files.write: [data, output]
    net: []
  writes: true
  check: check
---
# Research report from my own material

Give a question or a topic and the folders to read; get a short report with sections, where every statement points at the file and paragraph it came from, and a list of what the material did not answer.

It reads only your own material: documents, notes and pages you saved in the folders you name. It does not search or fetch from the web. A skill that reaches the internet by itself needs the sandbox, which is not built yet, and Muse refuses to run such a skill until then. The step that finds passages is written so that a web source can be added later without changing the rest.

How it works:

1. Plan. A small model turns your question into 3 to 6 sub-questions. If its reply cannot be used, the question is split by simple rules instead.
2. Gather. On this computer, the passages most related to each sub-question are found in your folders (the same search index as "Answer over my files", kept in Muse's own folder).
3. Extract. A small model reads two passages at a time and lists the facts that help, each with the passage number it came from. This is most of the requests. Every fact is checked at once against the passage it cites, and dropped if it does not match.
4. Write. A strong model writes the report once, from the checked facts only; it does not see your documents again.
5. Check. Before you see it: every sentence that states a fact has a source, every source exists and shares words with the sentence, every sub-question is covered or listed as not answered, the length and language are as asked.

What is sent: your question, and the passages found for it, to models through Muse's router under the privacy class of the run. The default is standard: free services may learn from what is sent. Run with `--privacy private` or `--privacy local` to restrict that.

The budget, before it starts: at most 40 requests and 150,000 tokens in 15 minutes, and nothing paid. A run that would go over stops and gives you what it has.

If the strong model is not free right now, you get exactly the choices the router offers (wait until a time, hand off to your own ChatGPT, Claude or Gemini app, or pay about an amount) together with the checked facts by sub-question, marked "Not done yet: the written report", and a prompt you can paste into your own app to have the report written there.

What it cannot do: use the web, read scanned images, or judge whether a document is right. The check compares words, so it catches a missing or wrong source, not a subtle misreading; a report in a different language from the documents it cites usually fails the check.

Inputs: `question` or `topic` (required); `outline` (parts you want, separated by `;`); `length` (`short` or `medium`, default short); `lang` (`zh`, `en`, default: the language of the question); `embeddings` (`auto` or `off`).
