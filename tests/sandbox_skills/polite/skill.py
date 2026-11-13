"""A well-behaved fixture skill: every part of the context, in one run. Driven by `ctx.inputs["mode"]`."""

from __future__ import annotations

import json

from . import helper


def run(ctx):
    mode = ctx.inputs.get("mode", "all")
    if mode == "stop":
        ctx.stop("Give   a question.")
    if mode == "crash":
        return helper.divide(1, 0)
    if mode == "bad-path":
        ctx.workspace.read_text("/etc/hostname")
    if mode == "unserialisable":
        return {"thing": object()}
    files = sorted(info.path.rsplit("/", 1)[-1] for info in ctx.workspace.list())
    words = sum(len(ctx.workspace.read_text(info.path).split()) for info in ctx.workspace.list())
    ctx.event("read", files=len(files), words=words, text="x" * 200)
    counter = ctx.data_dir / "runs.json"
    runs = json.loads(counter.read_text())["runs"] + 1 if counter.exists() else 1
    counter.write_text(json.dumps({"runs": runs}))
    left = ctx.llm.remaining()
    if mode == "keep":
        ctx.keep({"text": "kept GOOD", "partial": True})
    if mode == "catch":
        try:
            ctx.llm.ask([{"role": "user", "content": "first"}])
        except ctx.llm.Deferred as exc:
            ctx.note(f"deferred: {exc.reason}", incomplete=True)
            return {"text": "GOOD without the model", "retry_after": exc.retry_after, "options": exc.options.as_list()}
    if mode == "bad-args":
        try:
            ctx.llm.ask([{"role": "user", "content": "x"}], max_tokens=0)
        except ValueError as exc:
            return {"text": "GOOD", "error": str(exc)}
    reply = ctx.llm.ask([{"role": "user", "content": ctx.inputs.get("q", "hello")}], max_tokens=50)
    vectors = ctx.llm.embed(["one", "two"]) if mode == "embed" else []
    ctx.note("Read   the files.")
    if mode == "partial":
        ctx.note("One file could not be read.", incomplete=True)
    if mode == "propose-outside":
        ctx.workspace.propose_write("/etc/muse-sandbox-test", "x", "outside")
    proposed = ctx.workspace.propose_write("note.md", f"# Note\n\n{reply.text}\n", "a note")
    return {"text": reply.text, "task": reply.task, "provider": reply.provider, "files": files, "words": words, "runs": runs,
            "attempt": ctx.attempt, "requests_left": left["requests"], "vectors": len(vectors), "proposed": proposed.path,
            "tuple": (1, 2), "privacy": ctx.privacy, "inputs": dict(ctx.inputs)}


def check(output, ctx):
    if ctx.inputs.get("mode") == "check-crash":
        raise KeyError("boom")
    return [] if "GOOD" in output["text"] else ["the answer lacks the word the test looks for"]
