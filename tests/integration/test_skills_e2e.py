"""`muse skills run files-answer` against the REAL proxy (muse.auth + muse.hook) and the fake upstream.

Catalogue: ration_catalog.build_rules_catalog. `everyday` is the alias `cheap`:
free B first, the one-off trial T behind it (held). There is no `embed` alias,
so the skill must fall back to searching by words without failing.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

from muse import apps, cli
from test_rules_pace_e2e import ledger, models, start, wait_for

CANARY = "CANARY-E2E-FILE-3c9e77"
ANSWER = json.dumps({"answer": "The monthly rent is 18,500 dollars [1].",
                     "claims": [{"text": "The monthly rent is 18,500 dollars", "cites": [1]}], "not_found": False})


@pytest.fixture()
def docs(tmp_path) -> Path:
    folder = tmp_path / "docs"
    folder.mkdir()
    (folder / "lease.md").write_text("# Lease\n\nThe monthly rent is 18,500 dollars, due on the 5th.\n")
    (folder / "garden.txt").write_text(f"Water the basil every morning. {CANARY}\n")
    return folder


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


def test_files_answer_through_the_real_router(make_proxy, upstream, docs, tmp_path, monkeypatch, capsys):
    proxy = start(make_proxy)
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / "skills.env"))
    monkeypatch.setenv("MUSE_PROXY_URL", proxy.url)
    monkeypatch.setenv("MUSE_HOME", str(proxy.home))
    upstream.set_reply("b-model", ANSWER)
    base = ("skills", "run", "files-answer", "--folder", str(docs), "--input", "question=How much is the monthly rent?")

    # The skill's own class is `private`. No provider here may take that: the router refuses, and nothing is downgraded.
    code, out, err = run_cli(capsys, *base)
    assert code == 1 and out == "" and "Refused for privacy" in err and "did not lower the privacy class" in err
    assert models(upstream) == []  # nothing left the router, not even an embedding

    code, out, err = run_cli(capsys, *base, "--privacy", "standard", "--json")
    result = json.loads(out)
    assert code == 0 and result["status"] == "done" and result["check"] == {"status": "passed", "reasons": []}
    assert result["output"]["text"] == "The monthly rent is 18,500 dollars [1].\n\nSources:\n[1] lease.md › paragraph 2"
    assert result["output"]["retrieval"]["mode"] == "lexical"  # no embed alias: words only, and the run still finished
    answered = [e for e in result["events"] if e["kind"] == "call" and e.get("ok")]
    assert len(answered) == 1
    assert answered[0]["task"] == "everyday"  # the task the router applied (its x-muse-task response header)
    assert answered[0]["provider"] == "freeb" and answered[0]["privacy"] == "standard" and answered[0]["cost_usd"] == 0
    assert models(upstream) == ["b-model"]  # everyday -> `cheap` -> free B; the strong, paid and one-off models were not touched
    assert upstream.calls()[0].path.endswith("/chat/completions") and upstream.calls()[0].max_tokens == 900

    # Nothing was charged, and the usage ledger knows the app but none of the text.
    assert result["used"]["cost_usd"] == 0 and result["used"]["providers"]["freeb"]["requests"] == 1
    rows = wait_for(lambda: [r for r in ledger(proxy).recent(50) if r["ok"] == 1 and r["app"] == "skills"])
    assert len(rows) == 1 and rows[0]["priority"] == "normal" and rows[0]["paid"] in (0, None) and not rows[0]["cost_usd"]
    assert ledger(proxy).paid_spend()["calls"] == 0
    registry = apps.load_apps({"MUSE_HOME": str(proxy.home)})
    assert registry["skills"].allow_paid is False and "skills-paid" not in registry
    data = (Path(proxy.home) / "ledger.sqlite").read_bytes()
    assert CANARY.encode() not in data and b"18,500" not in data and b"monthly rent" not in data
    assert CANARY not in proxy.log_text() and "18,500" not in proxy.log_text()

    # The plain form: the answer on stdout, one line about who answered and what it cost on stderr.
    code, out, err = run_cli(capsys, *base, "--privacy", "standard")
    assert code == 0 and out.startswith("The monthly rent is 18,500 dollars [1].")
    assert "Answered by freeb. Nothing spent." in err
