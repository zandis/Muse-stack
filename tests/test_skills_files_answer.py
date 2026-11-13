"""The shipped skill `files-answer`: index, retrieve, one model call, the citation check.

No network and no real provider: the router is queue_fakes.FakeProxy with scripted answers.
"""

from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

import pytest

from muse import cli
from muse.skills import loader, runner
from muse.skills.workspace import Workspace
from queue_fakes import embeddings, privacy_403, ration_429
from test_skills_support import chat, env, proxy  # noqa: F401  (fixtures)

CANARY = "CANARY-FILE-CONTENT-5d81c2"


def module(name: str):
    return loader.import_skill(loader.find("files-answer"), name)


@pytest.fixture()
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "lease.md").write_text(
        "# Lease\n\nThe flat is at 12 Harbour Street.\n\n"
        "The monthly rent is 18,500 dollars, due on the 5th of each month.\n\n"
        "The deposit equals two months of rent and is returned within 30 days of moving out.\n", encoding="utf-8")
    (folder / "garden.txt").write_text(
        f"Tomatoes need six hours of sun.\n\nWater the basil every morning. {CANARY}\n", encoding="utf-8")
    (folder / "report.md").write_text(
        "# 第三季報告\n\n第三季的營收達到 4.2 億元，比去年同期成長 12%。\n\n"
        "主要成長來自海外市場，尤其是日本與東南亞。\n\n下一季的重點是降低物流成本。\n", encoding="utf-8")
    return folder


def answer_json(answer: str, claims: list[tuple[str, list[int]]], not_found: bool = False) -> str:
    return json.dumps({"answer": answer, "claims": [{"text": t, "cites": c} for t, c in claims], "not_found": not_found},
                      ensure_ascii=False)


def ask(env, question: str, folder: Path, privacy: str | None = "standard", **inputs):
    inputs.setdefault("embeddings", "off")
    return runner.run("files-answer", {"question": question, **inputs}, folders=[folder], environ=env, privacy=privacy)


def chats(proxy) -> list[dict]:
    return [r for r in proxy.requests if r["path"].endswith("/chat/completions")]


def embeds(proxy) -> list[dict]:
    return [r for r in proxy.requests if r["path"].endswith("/embeddings")]


# ================================================================ words, BM25, overlap


def test_tokens_work_for_english_and_for_chinese_without_segmentation():
    tw = module("textwork")
    assert tw.tokens("The invoices are DUE on the 5th") == ["invoice", "due", "5th"]  # stop words out, plural folded
    assert tw.tokens("營收成長") == ["營收", "收成", "成長"]  # character pairs
    assert tw.tokens("第三季營收為4.2億元") == ["第三", "三季", "季營", "營收", "收為", "4.2", "億元"]
    assert tw.tokens("１２３ ＡＢＣ") == ["123", "abc"]  # full-width forms match plain ones
    assert tw.tokens("rent is 18,500 dollars") == ["rent", "18500", "dollar"]
    assert tw.tokens("貓") == ["貓"] and tw.tokens("... !!!") == []
    assert tw.mostly_chinese("第三季的營收是多少？") and not tw.mostly_chinese("What was Q3 revenue?")


def test_bm25_ranking_sanity():
    r, tw = module("retrieve"), module("textwork")
    docs = [
        "The monthly rent is 18,500 dollars, due on the 5th.",
        "Tomatoes need six hours of sun. Water the basil every morning.",
        "The deposit equals two months of rent. " + "The lease also covers parking, storage and the garden shed. " * 6,
        "rent rent rent",
    ]
    scores = r.bm25(tw.tokens("how much is the monthly rent"), [tw.tokens(d) for d in docs])
    order = r.ranked(scores)
    assert order[0] == 0  # both query words, short
    assert 1 not in order and scores[1] == 0  # no shared term: not ranked at all
    assert scores[3] > scores[2]  # the same word in a much longer passage counts for less
    # A rare word outweighs a common one.
    corpus = ["rent due", "rent paid", "rent deposit", "rent harbour"]
    scores = r.bm25(tw.tokens("rent harbour"), [tw.tokens(d) for d in corpus])
    assert r.ranked(scores)[0] == 3
    assert r.bm25(["x"], []) == [] and r.ranked([0.0, 0.0]) == []
    zh = ["第三季的營收達到 4.2 億元", "下一季的重點是降低物流成本", "主要成長來自海外市場"]
    assert r.ranked(r.bm25(tw.tokens("物流成本怎麼降低？"), [tw.tokens(d) for d in zh]))[0] == 1


def test_fusion_and_cosine():
    r = module("retrieve")
    assert r.fuse([[1, 2, 3], [3, 1, 4]])[:2] == [1, 3]  # high in both lists beats high in one
    assert r.fuse([[5, 6]]) == [5, 6] and r.fuse([]) == []
    assert r.cosine([1, 0], [1, 0]) == pytest.approx(1.0) and r.cosine([1, 0], [0, 1]) == 0 and r.cosine([1], [1, 2]) == 0
    assert r.unpack(r.pack([0.5, -1.0, 2.0])) == [0.5, -1.0, 2.0]


def test_bigram_overlap_for_chinese_and_english():
    tw = module("textwork")
    shared, share = tw.overlap("第三季營收為4.2億元", "第三季的營收達到 4.2 億元，比去年同期成長 12%。")
    assert shared == 5 and share == pytest.approx(5 / 7)  # 第三 三季 營收 4.2 億元
    assert tw.overlap("下一季會擴大招募工程師", "第三季的營收達到 4.2 億元")[1] < 0.15
    shared, share = tw.overlap("The rent is 18,500 dollars a month", "The monthly rent is 18,500 dollars, due on the 5th.")
    assert shared == 3 and share == pytest.approx(0.75)  # rent, 18500, dollar; "month" is not "monthly"
    assert tw.overlap("Basil needs water every morning", "The monthly rent is 18,500 dollars")[0] == 0
    assert tw.overlap("the of and", "anything") == (0, 0.0)


# ================================================================ reading the model's reply


def test_parse_reads_clean_fenced_and_wrapped_json():
    a = module("answer")
    body = answer_json("Rent is 18,500 [1].", [("Rent is 18,500 dollars", [1])])
    for raw in (body, f"```json\n{body}\n```", f"Sure! Here is the JSON you asked for:\n\n{body}\n\nHope this helps.",
                f"```\n{body}\n```\nLet me know if you need more."):
        parsed = a.parse(raw)
        assert parsed["read"] == "json" and parsed["answer"] == "Rent is 18,500 [1]." and parsed["not_found"] is False
        assert parsed["claims"] == [{"text": "Rent is 18,500 dollars", "cites": [1]}]


def test_parse_repairs_a_truncated_object():
    a = module("answer")
    whole = answer_json("Rent is 18,500 [1]. Deposit is two months [2].",
                        [("Rent is 18,500 dollars", [1]), ("The deposit equals two months of rent", [2])])
    cut = a.parse(whole[:whole.index("two months of rent") + 5])  # stopped inside the second claim's text
    assert cut["read"] == "repaired" and cut["claims"][0] == {"text": "Rent is 18,500 dollars", "cites": [1]}
    assert a.parse(whole[:whole.index('"claims"') + 12])["read"] == "repaired"
    assert a.parse('{"answer": "Rent is 18,500 [1]')["claims"] == [{"text": "Rent is 18,500", "cites": [1]}]
    assert a.parse('{"answer": "x [1]", "claims": [{"text": "x", "cites": [1]},], "not_found": false,}')["read"] == "json"


def test_parse_tolerates_the_shapes_small_models_use():
    a = module("answer")
    odd = a.parse(json.dumps({"answer": "A", "claims": [{"claim": "one [2]"}, {"text": "two", "cites": "[1], [3]"},
                                                        {"text": "three", "citations": ["2"]}, "four [1]", 7], "not_found": "false"}))
    assert odd["claims"] == [{"text": "one", "cites": [2]}, {"text": "two", "cites": [1, 3]}, {"text": "three", "cites": [2]},
                             {"text": "four", "cites": [1]}]
    assert a.parse('{"answer": "", "claims": [], "not_found": "true"}')["not_found"] is True
    plain = a.parse("The rent is 18,500 dollars [1]. The deposit is two months of rent [2][3]. I hope that helps!")
    assert plain["read"] == "text" and plain["claims"] == [
        {"text": "The rent is 18,500 dollars", "cites": [1]}, {"text": "The deposit is two months of rent", "cites": [2, 3]},
        {"text": "I hope that helps!", "cites": []}]  # words after the last marker cite nothing, and the check will say so
    assert a.parse("租金是 18,500 元【1】。")["claims"] == [{"text": "租金是 18,500 元", "cites": [1]}]
    assert a.parse("")["read"] == "empty" and a.parse("   ")["claims"] == []
    derived = a.parse('{"answer": "Rent is 18,500 [1].", "not_found": false}')  # no claims list: taken from the answer
    assert derived["claims"] == [{"text": "Rent is 18,500", "cites": [1]}]
    assert a.parse('{"unrelated": 1}')["read"] == "text"


# ================================================================ the check


PASSAGES = [{"n": 1, "text": "The monthly rent is 18,500 dollars, due on the 5th of each month."},
            {"n": 2, "text": "Tomatoes need six hours of sun."},
            {"n": 3, "text": "第三季的營收達到 4.2 億元，比去年同期成長 12%。"}]


def out(claims, not_found=False, answer="x"):
    return {"answer": answer, "claims": [{"text": t, "cites": c} for t, c in claims], "not_found": not_found, "passages": PASSAGES}


def test_citation_labels():
    label = module("skill")._label
    assert label("a/b.md", [3], False) == "a/b.md › paragraph 3" and label("a/b.md", [5, 3, 3], False) == "a/b.md › paragraph 3, 5"
    assert label("b.md", [2, 3, 4], False) == "b.md › paragraph 2-4" and label("報告.md", [2, 5], True) == "報告.md › 第 2、5 段"


def test_the_check_passes_cited_overlapping_claims_in_both_languages():
    check = module("skill").check
    assert check(out([("The rent is 18,500 dollars a month", [1]), ("第三季營收為4.2億元", [3])])) == []
    assert check(out([], not_found=True, answer="Not in your files.")) == []


def test_the_check_catches_an_uncited_claim_and_a_wrong_citation():
    check = module("skill").check
    reasons = check(out([("The rent is 18,500 dollars a month", [1]), ("The landlord is called Mr Lin", [])]))
    assert reasons == ["claim 2 cites no passage"]
    assert check(out([("The rent is 18,500 dollars a month", [9])])) == ["claim 1 cites passage 9, which was not given"]
    wrong = check(out([("The rent is 18,500 dollars a month", [2])]))  # the passage exists but says nothing of the kind
    assert wrong == ["claim 1 shares too little with passage 2"]
    assert check(out([("第三季營收為4.2億元", [1])])) == ["claim 1 shares too little with passage 1"]
    both = check(out([("The rent is 18,500 dollars a month", [1, 2])]))  # every cited passage must support it
    assert both == ["claim 1 shares too little with passage 2"]


def test_the_check_catches_not_found_with_claims_and_empty_answers():
    check = module("skill").check
    assert check(out([("The rent is 18,500 dollars a month", [1])], not_found=True)) == [
        "the answer says it was not found but still makes claims"]
    assert check(out([], answer="")) == ["the answer is empty", "the answer makes no cited claims"]
    assert check(out([("the of and", [1])])) == ["claim 1 says nothing that can be checked"]
    assert check(None) == ["the skill returned no answer"]
    for reason in check(out([("The landlord, Mr Lin, lives upstairs with a parrot", [2])])):
        assert "Lin" not in reason and "parrot" not in reason  # reasons name claims by number, never quote them


# ================================================================ extracting and chunking


def test_paragraph_chunks_are_bounded_numbered_and_overlap():
    index, tw = module("index"), module("textwork")
    paras = [f"Paragraph {n} talks about topic {n} at some length, enough to weigh a little." for n in range(1, 41)]
    chunks = index.chunk(paras, limit=60, overlap=20)
    assert len(chunks) > 5 and chunks[0][0] == 1 and chunks[-1][1] == 40
    assert all(tw.weight(text) <= 60 + 20 for _, _, text, _ in chunks)  # bounded: the limit plus the overlap
    for (_, last, text, numbers), (first, _, following, next_numbers) in zip(chunks, chunks[1:]):
        assert first == last + 1  # numbering continues with the first NEW paragraph
        assert text.split("\n\n")[-1] == following.split("\n\n")[0]  # the previous chunk's tail opens the next
        assert next_numbers[0] == last and len(numbers) == len(text.split("\n\n"))  # each part knows its paragraph
    long = "這是一個很長的段落。" * 200
    pieces = index.chunk([long], limit=100, overlap=20)
    assert len(pieces) > 10 and all(a == b == 1 for a, b, _, _ in pieces) and all(tw.weight(t) <= 120 for _, _, t, _ in pieces)
    assert all(tw.weight(t) <= 120 for _, _, t, _ in index.chunk(["x" * 5000], limit=100, overlap=20))  # no sentence ends at all
    assert index.chunk([]) == []
    rows = ["name,amount"] + [f"item{n},{n * 10}" for n in range(200)]
    table = index.chunk(rows, limit=60, overlap=0, header=rows[0])
    assert all(text.startswith("name,amount") for _, _, text, _ in table)  # a table's header rides with every chunk
    assert table[1][3][0] == 0 and table[1][3][1] == table[1][0]  # the repeated header is paragraph 0: never cited


def test_extracting_text_from_each_supported_kind():
    index = module("index")
    html = "<html><head><style>p{color:red}</style><script>var secret=1</script></head><body><h1>Title</h1><p>First &amp; second.</p></body></html>"
    text, _ = index.extract(".html", html.encode())
    assert "Title" in text and "First & second." in text and "secret" not in text and "color" not in text
    assert index.paragraphs(text) == ["Title", "First & second."]
    text, by_line = index.extract(".json", b'{"rent": 18500, "city": "\xe5\x8f\xb0\xe5\x8c\x97"}')
    assert by_line and '"rent": 18500' in text and "台北" in text
    assert index.extract(".csv", b"a,b\n1,2\n") == ("a,b\n1,2\n", True)
    assert index.extract(".txt", "租金".encode("big5"))[0] == "租金"  # older Traditional Chinese files
    assert index.extract(".txt", "﻿hello".encode("utf-8"))[0] == "hello"
    assert index.extract(".txt", b"\x00\x01\x02binary")[0] is None
    assert index.extract(".pdf", b"%PDF-1.4", None)[0] is None
    assert index.extract(".pdf", b"%PDF-1.4", lambda data: "from the reader")[0] == "from the reader"
    assert index.paragraphs("a\r\n\r\n\r\nb   c\n\n") == ["a", "b c"]


# ================================================================ indexing: incremental, caps


def sync(tmp_path, folder, **caps):
    index = module("index")
    db = index.open_db(tmp_path)
    try:
        return index.sync(db, Workspace([folder]), index.Caps(**caps) if caps else None), index.load_chunks
    finally:
        db.close()


def test_indexing_is_incremental(tmp_path, docs, monkeypatch):
    reads: list[str] = []
    original = Workspace.read_bytes
    monkeypatch.setattr(Workspace, "read_bytes", lambda self, path, limit=0: (reads.append(Path(path).name), original(self, path, limit))[1])
    first, _ = sync(tmp_path, docs)
    assert (first.seen, first.read, first.reused) == (3, 3, 0) and sorted(reads) == ["garden.txt", "lease.md", "report.md"]
    reads.clear()
    second, _ = sync(tmp_path, docs)
    assert (second.seen, second.read, second.reused) == (3, 0, 3) and reads == []  # unchanged files are not opened
    assert second.chunks == first.chunks and second.file_ids == first.file_ids

    (docs / "lease.md").write_text("The rent went up to 19,000 dollars.\n")
    (docs / "garden.txt").unlink()
    (docs / "new.txt").write_text("A new note.\n")
    third, _ = sync(tmp_path, docs)
    assert sorted(reads) == ["lease.md", "new.txt"] and (third.read, third.reused, third.removed) == (2, 1, 1)
    db = sqlite3.connect(tmp_path / "index.sqlite")
    texts = " ".join(row[0] for row in db.execute("SELECT text FROM chunks"))
    paths = {Path(row[0]).name for row in db.execute("SELECT path FROM files")}
    db.close()
    assert "19,000" in texts and "18,500" not in texts and CANARY not in texts and paths == {"lease.md", "report.md", "new.txt"}
    assert os.stat(tmp_path / "index.sqlite").st_mode & 0o077 == 0  # the index holds file text: owner only


def test_only_the_folders_of_this_run_are_searched(tmp_path, docs):
    other = tmp_path / "other"
    other.mkdir()
    (other / "private.txt").write_text("A different folder, indexed in an earlier run.\n")
    index = module("index")
    db = index.open_db(tmp_path)
    earlier = index.sync(db, Workspace([other]))
    now = index.sync(db, Workspace([docs]))
    names = {Path(c.path).name for c in index.load_chunks(db, now.file_ids)}
    assert names == {"garden.txt", "lease.md", "report.md"} and earlier.file_ids.isdisjoint(now.file_ids)
    assert db.execute("SELECT count(*) FROM files").fetchone()[0] == 4  # the other folder's index is kept, not searched
    db.close()


def test_hidden_unsupported_and_unreadable_files(tmp_path, docs):
    (docs / ".hidden.txt").write_text("hidden")
    (docs / ".git").mkdir()
    (docs / ".git" / "config.txt").write_text("hidden too")
    (docs / "photo.jpg").write_bytes(b"\xff\xd8\xff")
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4")
    (docs / "blob.txt").write_bytes(b"\x00\x01\x02")
    stats, _ = sync(tmp_path, docs)
    assert (stats.seen, stats.unsupported, stats.pdf_skipped, stats.unreadable) == (4, 1, 1, 1)
    again, _ = sync(tmp_path, docs)
    assert again.read == 0 and again.unreadable == 1  # remembered: not read again, still reported


def test_caps_are_hard_and_reported(tmp_path, docs):
    for n in range(6):
        (docs / f"note{n}.txt").write_text(f"Note number {n}.\n\n" + "More words here. " * 50)
    stats, _ = sync(tmp_path / "a", docs, files=4) if (tmp_path / "a").mkdir() is None else None
    assert stats.seen == 4 and stats.hit_files and len(stats.file_ids) == 4
    (tmp_path / "b").mkdir()
    stats, _ = sync(tmp_path / "b", docs, chunks=3)
    assert stats.chunks == 3 and stats.hit_chunks
    (tmp_path / "c").mkdir()
    stats, _ = sync(tmp_path / "c", docs, file_bytes=200)
    assert stats.large == 6 and stats.read == 9 and len(stats.file_ids) == 3
    (tmp_path / "d").mkdir()
    stats, _ = sync(tmp_path / "d", docs, run_bytes=400)
    assert stats.hit_bytes and 0 < len(stats.file_ids) < 9
    more, _ = sync(tmp_path / "d", docs, run_bytes=100_000)  # the next run carries on where the cap stopped it
    assert not more.hit_bytes and len(more.file_ids) == 9


# ================================================================ end to end with the scripted router


def test_an_english_question_end_to_end(env, proxy, docs):
    proxy.push(*chat(answer_json("The rent is 18,500 dollars a month [1].", [("The monthly rent is 18,500 dollars", [1])]),
                     task="everyday", provider="groq"))
    result = ask(env, "How much is the monthly rent?", docs, privacy=None, top_k=2)
    assert result.status == "done" and result.check == {"status": "passed", "reasons": []}
    request = chats(proxy)[0]
    assert request["task"] == "everyday" and request["privacy"] == "standard"  # the skill's own defaults
    assert request["max_tokens"] == 900 and len(proxy.requests) == 1  # ONE model call
    sent = request["messages"][1]["content"]
    assert "[1] " in sent and "18,500 dollars" in sent and "How much is the monthly rent?" in sent
    assert CANARY not in json.dumps(request["body"])  # only the relevant passages leave, not the folder
    output = result.output
    assert output["not_found"] is False and output["claims"][0]["cites"] == [1]
    assert output["claims"][0]["sources"] == ["lease.md › paragraph 3"]  # the paragraph inside the passage, not just the file
    assert output["citations"] == [{"n": 1, "path": str(docs.resolve() / "lease.md"), "paragraphs": [3],
                                    "label": "lease.md › paragraph 3"}]
    assert output["text"] == "The rent is 18,500 dollars a month [1].\n\nSources:\n[1] lease.md › paragraph 3"
    assert output["passages"][0]["text"].startswith("# Lease") and output["passages"][0]["paragraphs"] == [1, 2, 3, 4]
    assert len(output["passages"]) == 1  # the other files share no word with the question, so they are not sent
    assert output["retrieval"] == {"mode": "lexical", "files": 3, "chunks": 3, "sent": 1}
    assert list(result.used["providers"]) == ["groq"] and result.used["cost_usd"] == 0


def test_a_traditional_chinese_question_end_to_end(env, proxy, docs):
    proxy.push(*chat("```json\n" + answer_json("第三季的營收是 4.2 億元，比去年同期成長 12% [1]。",
                                              [("第三季營收達到 4.2 億元", [1]), ("比去年同期成長 12%", [1])]) + "\n```", task="everyday"))
    result = ask(env, "第三季的營收是多少？", docs, top_k=1)
    assert result.status == "done" and result.check["status"] == "passed"
    sent = chats(proxy)[0]["messages"][1]["content"]
    assert "第三季的營收達到 4.2 億元" in sent and "第三季的營收是多少？" in sent and "Harbour" not in sent
    assert "Traditional Chinese" in chats(proxy)[0]["messages"][0]["content"]
    output = result.output
    assert output["text"].startswith("第三季的營收是 4.2 億元") and "來源：\n[1] report.md › 第 2 段" in output["text"]
    assert [c["cites"] for c in output["claims"]] == [[1], [1]] and output["read"] == "json"


def test_a_failed_check_retries_on_a_stronger_tier_and_can_pass(env, proxy, docs):
    proxy.push(*chat("The rent is 18,500 dollars and the landlord is friendly.", task="everyday"))  # no citations at all
    proxy.push(*chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="heavy"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert [r["task"] for r in chats(proxy)] == ["everyday", "heavy"]
    assert result.status == "done" and result.check["status"] == "passed" and result.output["claims"][0]["cites"] == [1]
    checks = [e for e in result.events if e["kind"] == "check"]
    assert [c["status"] for c in checks] == ["failed", "passed"]


def test_an_answer_that_fails_twice_is_marked_and_lists_the_failed_claims(env, proxy, docs):
    bad = answer_json("Rent is 18,500 [1]. The landlord is Mr Lin.",
                      [("The monthly rent is 18,500 dollars", [1]), ("The landlord is Mr Lin", []), ("Parking costs 900 dollars", [7])])
    proxy.push(*chat(bad, task="everyday"))
    proxy.push(*chat(bad, task="heavy"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert result.status == "needs_choice" and result.options == [] and len(chats(proxy)) == 2
    assert result.check == {"status": "failed", "reasons": ["claim 2 cites no passage", "claim 3 cites passage 7, which was not given"]}
    assert result.output["claims"][0]["cites"] == [1]  # the answer is still there, for the app to show as unchecked


def test_an_empty_reasoning_answer_fails_the_check_and_is_retried(env, proxy, docs):
    proxy.push(*chat("", task="everyday", **{"x-muse-answer": "empty-reasoning"}))
    proxy.push(*chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="heavy"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert result.status == "done" and [r["task"] for r in chats(proxy)] == ["everyday", "heavy"]
    assert next(e for e in result.events if e["kind"] == "skill.answer")["empty"] is True


def test_not_found_is_said_plainly(env, proxy, docs):
    proxy.push(*chat(answer_json("", [], not_found=True), task="everyday"))
    result = ask(env, "What is the rent for the garage in Harbour Street?", docs)
    assert result.status == "done" and result.output["not_found"] is True and result.output["claims"] == []
    assert result.output["text"] == "I could not find the answer to this in the files you named."
    # No words in common with any file: no model is asked at all.
    proxy.requests.clear()
    result = ask(env, "Zebra xylophone quartz?", docs)
    assert result.status == "done" and result.output["not_found"] is True and proxy.requests == []
    assert any("no model was asked" in n for n in result.notes) and result.used["requests"] == 0
    result = ask(env, "斑馬？", docs)
    assert result.output["text"] == "在你指定的資料夾裡找不到這個問題的答案。"


def test_missing_inputs_stop_before_anything_is_read(env, proxy, docs):
    result = runner.run("files-answer", {}, folders=[docs], environ=env)
    assert result.status == "failed" and "Give a question" in result.notes[0]
    result = runner.run("files-answer", {"question": "x"}, environ=env)
    assert result.status == "failed" and "Name at least one folder" in result.notes[0]
    assert proxy.requests == [] and not (Path(env["MUSE_HOME"]) / "skills-data").exists()


def test_a_privacy_refusal_is_returned_never_downgraded(env, proxy, docs):
    proxy.push(*privacy_403("local"))  # the embedding call
    proxy.push(*privacy_403("local"))  # the answer call
    result = ask(env, "How much is the monthly rent?", docs, privacy="local", embeddings="auto")
    assert result.status == "failed" and result.refusal == {"kind": "privacy", "privacy": "local"}
    assert [r["privacy"] for r in proxy.requests] == ["local", "local"]  # nothing was sent under a weaker class
    assert [r["path"].rsplit("/", 1)[-1] for r in proxy.requests] == ["embeddings", "completions"]


def test_a_deferred_answer_gives_the_routers_choices(env, proxy, docs):
    proxy.push(*ration_429("defer:reserve", 1800, "wait:2026-11-13T20:00:00Z,handoff"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert result.status == "needs_choice" and result.output is None
    assert result.options == [{"choice": "wait", "until": "2026-11-13T20:00:00Z"}, {"choice": "handoff"}]


# ================================================================ embeddings: hybrid, cached, skipped cleanly


def meaning(request):
    """A stand-in embedding model: one axis for money, one for plants."""
    if not request["path"].endswith("/embeddings"):
        return None
    vectors = []
    for text in request["input"]:
        money = any(word in text for word in ("rent", "deposit", "cost", "pay", "營收"))
        vectors.append([1.0, 0.1] if money else [0.1, 1.0])
    return 200, {"x-muse-provider": "cohere", "x-muse-deployment": "embed-v4", "x-muse-cost-usd": "0.000000"}, embeddings(vectors)


def test_hybrid_retrieval_embeds_candidates_once_and_reuses_them(env, proxy, docs):
    proxy.responder = lambda request: meaning(request) or chat(
        answer_json("You pay 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="everyday")
    result = ask(env, "What do I pay for the flat?", docs, embeddings="auto", top_k=1)
    assert result.status == "done" and result.output["retrieval"]["mode"] == "hybrid"
    first = embeds(proxy)
    assert [r["task"] for r in first] == ["embed", "embed"] and [r["model"] for r in first] == ["embed", "embed"]
    assert first[0]["input"] == ["What do I pay for the flat?"] and len(first[1]["input"]) == 3  # the question, then the candidates
    assert all(r["privacy"] == "standard" for r in proxy.requests)
    # "pay" and "flat" barely match by words; meaning puts the lease first.
    assert result.output["passages"][0]["label"].startswith("lease.md")
    assert result.used["requests"] == 3

    proxy.requests.clear()
    again = ask(env, "What do I pay for the flat?", docs, embeddings="auto", top_k=1)
    assert again.status == "done" and len(embeds(proxy)) == 1 and embeds(proxy)[0]["input"] == ["What do I pay for the flat?"]


def test_embeddings_are_skipped_cleanly_when_the_router_defers_or_has_none(env, proxy, docs):
    good = chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="everyday")
    for refusal in (ration_429("defer:pace", 900, "wait:2026-11-13T20:00:00Z"),
                    (400, {}, b'{"error": {"message": "Invalid model name passed in model=embed"}}')):
        proxy.requests.clear()
        proxy.push(*refusal)
        proxy.push(*good)
        result = ask(env, "How much is the monthly rent?", docs, embeddings="auto")
        assert result.status == "done" and result.output["retrieval"]["mode"] == "lexical" and result.notes == []
        assert len(embeds(proxy)) == 1 and len(chats(proxy)) == 1  # one try, then words only; the answer still comes
        skipped = next(e for e in result.events if e["kind"] == "skill.embed_skipped")
        assert skipped["reason"] in ("Deferred", "RouterError")


def test_embeddings_are_not_tried_when_the_budget_is_tight(env, proxy, docs):
    proxy.push(*chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="everyday"))
    result = runner.run("files-answer", {"question": "How much is the monthly rent?"}, folders=[docs], environ=env,
                        privacy="standard", budget={"requests": 3})
    assert result.status == "done" and embeds(proxy) == [] and result.used["requests"] == 1


# ================================================================ notes, PDFs, caps, and what never leaves


def test_pdfs_are_skipped_with_a_note_when_no_reader_is_installed(env, proxy, docs, monkeypatch):
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4 not really")
    monkeypatch.setattr(module("index"), "pdf_reader", lambda: None)
    proxy.push(*chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="everyday"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert result.status == "partial" and result.exit_code == 3 and result.check["status"] == "passed"
    assert result.notes == ["1 PDF file(s) were not read: no PDF reader is installed with Muse."]


def test_pdfs_are_read_when_a_reader_is_already_installed(env, proxy, docs, monkeypatch):
    (docs / "scan.pdf").write_bytes(b"%PDF-1.4 not really")
    monkeypatch.setattr(module("index"), "pdf_reader", lambda: (lambda data: "The parking space costs 900 dollars a month."))
    proxy.push(*chat(answer_json("Parking costs 900 dollars [1].", [("The parking space costs 900 dollars", [1])]), task="everyday"))
    result = ask(env, "What does the parking space cost?", docs, top_k=1)
    assert result.status == "done" and result.output["citations"][0]["label"] == "scan.pdf › paragraph 1"


def test_caps_make_the_result_partial_and_say_so(env, proxy, docs):
    proxy.push(*chat(answer_json("Tomatoes need six hours of sun [1].", [("Tomatoes need six hours of sun", [1])]), task="everyday"))
    result = ask(env, "How much sun do tomatoes need?", docs, max_files=1)
    assert result.status == "partial" and "Only the first 1 files were searched" in result.notes[0]
    assert result.output["retrieval"]["files"] == 1


def test_links_out_of_the_folder_are_not_followed(env, proxy, docs, tmp_path):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "diary.txt").write_text(f"The monthly rent secret is {CANARY}.\n")
    (docs / "diary.txt").symlink_to(secret / "diary.txt")
    (docs / "more").symlink_to(secret)
    proxy.push(*chat(answer_json("The rent is 18,500 dollars [1].", [("The monthly rent is 18,500 dollars", [1])]), task="everyday"))
    result = ask(env, "How much is the monthly rent?", docs)
    assert result.status == "done" and "2 link(s) pointing outside" in result.notes[0]
    assert CANARY not in json.dumps(chats(proxy)[0]["body"]) and result.output["retrieval"]["files"] == 3


def test_no_file_content_reaches_events_notes_usage_or_the_terminal_log(env, proxy, docs, capsys):
    (docs / "garden.txt").write_text(f"Tomatoes need six hours of sun. {CANARY}\n")
    proxy.responder = lambda request: meaning(request) or chat(
        answer_json("Tomatoes need six hours of sun [1].", [("Tomatoes need six hours of sun", [1])]), task="everyday")
    seen: list[dict] = []
    result = runner.run("files-answer", {"question": "How much sun do tomatoes need?", "top_k": "1"}, folders=[docs], environ=env,
                        privacy="standard", on_event=seen.append)
    assert result.status == "done" and CANARY in result.output["passages"][0]["text"]  # the output shows the passage ...
    assert CANARY in json.dumps(chats(proxy)[0]["body"])  # ... and it was sent to the model, as intended
    public = result.public()
    for part in ("events", "notes", "used", "check", "options"):  # ... but nothing else carries file text
        dumped = json.dumps(public[part], ensure_ascii=False)
        assert CANARY not in dumped and "Tomatoes" not in dumped and "six hours" not in dumped
    assert CANARY not in json.dumps(seen) and "tomatoes" not in json.dumps(seen).lower()
    kinds = [e["kind"] for e in result.events]
    assert kinds[0] == "start" and kinds[-1] == "end" and "skill.index" in kinds and "skill.retrieve" in kinds
    index_event = next(e for e in result.events if e["kind"] == "skill.index")
    assert index_event["seen"] == 3 and index_event["read"] == 3 and index_event["chunks"] == 3
    # Outside the skill's own index (which is the text, by design) and the answer, MUSE_HOME holds none of it.
    home = Path(env["MUSE_HOME"])
    for path in home.rglob("*"):
        if path.is_file() and path.parent != home / "skills-data" / "files-answer":
            assert CANARY.encode() not in path.read_bytes(), path
    # The command line prints the answer on stdout and only route facts and notes on stderr.
    code = cli.main(["skills", "run", "files-answer", "--folder", str(docs), "--privacy", "standard",
                     "--input", "question=How much sun do tomatoes need?", "--input", "top_k=1"])
    captured = capsys.readouterr()
    assert code == 0 and captured.out.startswith("Tomatoes need six hours of sun [1].\n\nSources:\n[1] garden.txt › paragraph 1")
    assert "Answered by cohere and freea. Nothing spent." in captured.err and CANARY not in captured.err + captured.out
