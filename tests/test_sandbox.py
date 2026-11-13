"""The skill sandbox: everything that needs no special privilege.

The control pipe, the scrubbed environment, closed descriptors, resource limits, timeouts and the kill of the whole
process group, budget and privacy still enforced by the parent, proposed writes returned as data, the same `Result`
as the in-process runner, the modes (`off`, `auto`, `require`) and the rule for skills that do not ship with Muse.

The escape attempts against each isolation layer are in tests/test_sandbox_escape.py.
No network and no real provider: the router is queue_fakes.FakeProxy.
"""

from __future__ import annotations

import dataclasses
import json
import os
import random
import re
import shutil
import struct
import sys
import threading
import time
from pathlib import Path

import pytest

from muse import cli, sandbox
from muse.sandbox import Capabilities, Policy, Report, launch, lockdown, protocol
from muse.sandbox import host as sandbox_host
from muse.sandbox.policy import mode_for
from muse.skills import loader, runner
from queue_fakes import embeddings, privacy_403, ration_429
from test_sandbox_support import (  # noqa: F401
    CANARY_ENV, CANARY_HOME, CANARY_OUTSIDE, FIXTURES, LAYERS, LINUX, attempt, box, caps, chat, env, need, proxy, real_home_skills, run,
)
from test_skills_support import write_skill

posix = pytest.mark.skipif(sys.platform == "win32", reason="the sandbox starts no child process on Windows yet")


def plain(result) -> dict:
    """A result as comparable data: no timings, no sandbox report."""
    data = json.loads(json.dumps(result.public(), default=str))
    data.pop("sandbox", None)
    data["used"].pop("seconds", None)
    data["events"] = [{k: v for k, v in e.items() if k not in ("t", "ms")} for e in data["events"] if not e["kind"].startswith("sandbox")]
    # A skill may name its output file after the clock (report-20261113-165339-...); two runs can straddle a second.
    for write in data.get("writes") or []:
        if isinstance(write, dict) and isinstance(write.get("path"), str):
            write["path"] = re.sub(r"-\d{8}-\d{6}-", "-<when>-", write["path"])
    return data


def both_ways(box, proxy, name, inputs, script, layer="all", **options):
    """Run a fixture skill in-process, then sandboxed, against the same scripted router. Returns (plain, plain, report)."""
    outs = []
    report = None
    for mode in ("off", "auto"):
        shutil.rmtree(box.home / "skills-data" / name, ignore_errors=True)
        proxy.script.clear()
        for item in script:
            proxy.push(*item)
        result, report = run(box, name, dict(inputs), layer, mode=mode, **options)
        outs.append(plain(result))
    return outs[0], outs[1], report


# ================================================================ records and the mode rule


def test_mode_rule_shipped_follows_the_setting_and_anything_else_requires(monkeypatch):
    real_home_skills(monkeypatch)
    assert mode_for("shipped", None, {}) == "off"
    assert mode_for("shipped", None, {"MUSE_SANDBOX": "auto"}) == "auto"
    assert mode_for("shipped", "require", {"MUSE_SANDBOX": "off"}) == "require"  # the call's own choice wins over the setting
    for requested, setting in ((None, ""), ("off", "off"), ("auto", "auto"), (None, "off")):
        assert mode_for("home", requested, {"MUSE_SANDBOX": setting}) == "require"
    with pytest.raises(ValueError, match="off, auto, require"):
        mode_for("shipped", "maybe", {})
    with pytest.raises(ValueError):
        mode_for("home", None, {"MUSE_SANDBOX": "yes please"})


def test_the_test_only_way_out_is_the_process_environment_with_one_exact_value(monkeypatch):
    monkeypatch.setenv("MUSE_SANDBOX_UNSAFE_HOME_SKILLS", "1")
    assert mode_for("home", "off", {}) == "require"
    monkeypatch.setenv("MUSE_SANDBOX_UNSAFE_HOME_SKILLS", "in-process-for-tests")
    assert mode_for("home", "off", {}) == "off"
    monkeypatch.delenv("MUSE_SANDBOX_UNSAFE_HOME_SKILLS")
    assert mode_for("home", "off", {"MUSE_SANDBOX_UNSAFE_HOME_SKILLS": "in-process-for-tests"}) == "require"  # not from a passed mapping


def test_capabilities_say_what_require_needs():
    nothing = Capabilities(platform="linux")
    assert not nothing.can_require and len(nothing.missing()) == 2
    assert Capabilities(platform="linux", bwrap_works=True).can_require
    landlock_only = Capabilities(platform="linux", landlock_works=True, landlock_abi=7)
    assert landlock_only.fs_isolation and not landlock_only.net_isolation and not landlock_only.can_require  # TCP-only is not "no network"
    assert Capabilities(platform="linux", landlock_works=True, landlock_abi=7, seccomp_works=True).can_require
    old = Capabilities(platform="linux", landlock_works=True, landlock_abi=2, seccomp_works=True)
    assert not old.fs_isolation  # before ABI 3 a readable file can still be emptied
    mac = Capabilities(platform="darwin", sandbox_exec_path="/usr/bin/sandbox-exec")
    assert not mac.can_require and any("UNTESTED" in line for line in mac.lines())
    assert any("no isolation" in line for line in Capabilities(platform="win32").lines())
    assert json.dumps(nothing.public())


def test_report_is_isolated_only_with_files_and_network_both():
    report = Report(mode="auto", ran="sandboxed", landlock_abi=7, landlock_full=True, files="isolated", net="tcp blocked",
                    no_new_privs=True, rlimits={"AS": 1})
    assert not report.isolated and "NOT fully isolated" in report.sentence()
    assert report.layers() == ["landlock: abi 7", "no_new_privs", "rlimits", "files: isolated", "net: tcp blocked"]
    report.net, report.bwrap, report.seccomp = "blocked", True, True
    assert report.isolated and report.sentence().startswith("Ran isolated: bwrap, landlock: abi 7, seccomp")
    assert Report(mode="off").layers() == [] and "without isolation" in Report(mode="off", reason="switched off").sentence()
    assert Report(ran="refused", reason="no layers").sentence() == "Not run: no layers"
    best = Report(ran="sandboxed", landlock_abi=1, files="best effort")
    assert "landlock: abi 1 (best effort)" in best.layers() and not best.isolated


def test_probe_reports_this_machine_and_never_raises():
    c = sandbox.probe()
    assert c is sandbox.probe()  # kept for the process
    assert c.platform == ("linux" if LINUX else sys.platform)
    if c.bwrap_works:
        assert c.bwrap_path and "bubblewrap" in c.bwrap_version
    else:
        assert c.bwrap_reason
    if c.landlock_abi:
        assert c.landlock_works or c.landlock_reason
    assert c.lines()[0].startswith("platform:")
    print("\nsandbox capabilities here:\n  " + "\n  ".join(c.lines()))


# ================================================================ the control pipe


def pipe_channel(max_frame=1 << 16):
    r, w = os.pipe()
    back_r, back_w = os.pipe()
    return protocol.Channel(r, back_w, max_frame), w, back_r


def soon(seconds=2.0):
    return time.monotonic() + seconds


def test_frames_round_trip_with_bytes_and_unicode():
    chan, w, back_r = pipe_channel()
    os.write(w, protocol.encode({"op": "x", "text": "第三季 ✓", "data": protocol.b64(b"\x00\xff")}))
    frame = chan.recv(soon())
    assert frame["text"] == "第三季 ✓" and protocol.unb64(frame["data"]) == b"\x00\xff"
    chan.send({"op": "reply", "n": 1}, soon())
    (length,) = struct.unpack(">I", os.read(back_r, 4))
    assert json.loads(os.read(back_r, length)) == {"op": "reply", "n": 1}


@pytest.mark.parametrize("payload, code", [
    (b"not json at all", "not-json"),
    (b"\xff\xfe\x00", "not-json"),
    (b"[1, 2, 3]", "not-a-frame"),
    (b'"text"', "not-a-frame"),
    (b"{}", "not-a-frame"),
    (b'{"op": 5}', "not-a-frame"),
    (b'{"op": ""}', "not-a-frame"),
    (b'{"op": "' + b"x" * 60 + b'"}', "not-a-frame"),
    (b"[" * 30000 + b"]" * 30000, "too-deep"),
])
def test_malformed_frames_are_refused_with_a_code_never_content(payload, code):
    chan, w, _ = pipe_channel()
    os.write(w, struct.pack(">I", len(payload)) + payload)
    with pytest.raises(protocol.ProtocolError) as err:
        chan.recv(soon())
    assert str(err.value) == code


def test_an_oversized_frame_is_refused_before_it_is_read():
    chan, w, _ = pipe_channel(max_frame=1024)
    os.write(w, struct.pack(">I", 0x7FFFFFFF))  # claims two gigabytes; nothing follows
    with pytest.raises(protocol.ProtocolError, match="frame-too-large"):
        chan.recv(soon())
    assert chan.bytes_in == 4


def test_a_silent_peer_times_out_and_a_closed_peer_is_noticed():
    chan, w, _ = pipe_channel()
    started = time.monotonic()
    with pytest.raises(protocol.ChannelTimeout):
        chan.recv(soon(0.2))
    assert time.monotonic() - started < 2
    os.write(w, struct.pack(">I", 50) + b'{"op": "half')  # a frame cut off in the middle
    os.close(w)
    with pytest.raises(protocol.ChannelClosed):
        chan.recv(soon())


def test_a_peer_that_never_reads_cannot_block_the_parent_forever():
    chan, _w, _back_r = pipe_channel(max_frame=1 << 26)
    with pytest.raises(protocol.ChannelTimeout):
        chan.send({"op": "big", "data": "x" * (4 << 20)}, soon(0.3))  # far more than a pipe holds


def test_random_bytes_never_get_past_the_decoder():
    rng = random.Random(20261113)
    accepted = 0
    for _ in range(400):
        chan, w, _ = pipe_channel(max_frame=4096)
        blob = bytes(rng.randrange(256) for _ in range(rng.randrange(1, 200)))
        os.write(w, blob)
        os.close(w)
        try:
            frame = chan.recv(soon(0.5))
            accepted += 1
            assert isinstance(frame, dict) and isinstance(frame["op"], str)
        except (protocol.ProtocolError, protocol.ChannelClosed):
            pass
        finally:
            chan.close()
    assert accepted == 0  # 400 random blobs, none of them a frame


def test_the_ticker_runs_while_waiting_and_may_end_the_wait():
    calls = []

    def tick():
        calls.append(1)
        if len(calls) >= 3:
            raise RuntimeError("over the cap")

    r, _w = os.pipe()
    _back_r, back_w = os.pipe()
    chan = protocol.Channel(r, back_w, 1024, tick=tick, tick_every=0.02)
    with pytest.raises(RuntimeError, match="over the cap"):
        chan.recv(soon(5))


# ================================================================ the plan: environment, command, paths


def test_the_child_environment_is_an_allow_list():
    parent = {"PATH": "/evil/bin", "HOME": "/home/ann", "GROQ_API_KEY": CANARY_ENV, "LITELLM_MASTER_KEY": CANARY_ENV,
              "MUSE_HOME": "/home/ann/.muse", "MUSE_SKILLS_APP_TOKEN": CANARY_ENV, "LANG": "zh_TW.UTF-8", "TZ": "Asia/Taipei",
              "MUSE_TZ": "Asia/Taipei", "LC_ALL": "x" * 500, "PYTHONPATH": "/evil", "LD_PRELOAD": "/evil.so", "SSH_AUTH_SOCK": "/run/x"}
    child = launch.child_env(Policy(), parent, "/sandbox-tmp")
    assert child == {"PATH": "/usr/bin:/bin", "HOME": "/sandbox-tmp/home", "TMPDIR": "/sandbox-tmp", "LANG": "zh_TW.UTF-8",
                     "TZ": "Asia/Taipei", "MUSE_TZ": "Asia/Taipei"}
    assert CANARY_ENV not in json.dumps(child)


def test_the_child_command_is_isolated_python_and_bwrap_shares_only_named_paths():
    argv = launch.python_argv("/repo", (7, 9))
    assert argv[0] == sys.executable and argv[1:5] == ["-I", "-B", "-X", "utf8"] and argv[-3:] == ["/repo", "7", "9"]
    args = launch.bwrap_argv("/usr/bin/bwrap", {"PATH": "/usr/bin:/bin"}, ["/opt/python"], "/repo/muse", ["/home/ann/notes"],
                             "/home/ann/.muse/skills-data/x", [], 1000)
    text = " ".join(args)
    for option in ("--unshare-all", "--die-with-parent", "--new-session", "--clearenv"):
        assert option in args
    assert "--ro-bind /home/ann/notes /home/ann/notes" in text and "--bind /home/ann/.muse/skills-data/x /home/ann/.muse/skills-data/x" in text
    assert "--ro-bind /repo/muse /repo/muse" in text and "--size 1000 --tmpfs /sandbox-tmp" in text
    assert "--proc" not in args and "/sys" not in text and "--share-net" not in args
    assert " /home/ann " not in text + " " and "--bind / /" not in text and "--ro-bind / /" not in text
    assert "--size" not in launch.bwrap_argv("/usr/bin/bwrap", {}, [], "/repo/muse", [], None, [], None)


def test_path_sets_that_expose_muse_home_or_the_whole_disk_are_refused(tmp_path):
    home = tmp_path / "ann" / ".muse"
    home.mkdir(parents=True)
    ok = [str(tmp_path / "ann" / "notes")]
    launch.check_paths(ok, ["/usr/lib/python3"], str(home), "/repo")
    for roots in (["/"], [str(tmp_path / "ann")], [str(home)], [str(tmp_path)]):
        with pytest.raises(launch.Unavailable):
            launch.check_paths(roots, [], str(home), "/repo")
    with pytest.raises(launch.Unavailable, match="Python"):
        launch.check_paths(ok, [str(tmp_path / "ann")], str(home), "/repo")
    launch.check_paths([str(home / "skills-data" / "x")], [], str(home), "/repo")  # a folder INSIDE the home is not the home


def test_macos_profile_denies_by_default_and_is_marked_untested():
    profile = launch.macos_profile(["/opt/python"], "/repo/muse", ['/Users/ann/My "Notes"'], "/Users/ann/.muse/skills-data/x", "/tmp/t")
    lines = profile.splitlines()
    assert lines[:3] == ["(version 1)", "(deny default)", "(deny network*)"]
    assert '(subpath "/Users/ann/My \\"Notes\\"")' in profile  # quotes in a folder name cannot break out of the string
    write = next(line for line in lines if line.startswith("(allow file-write* "))
    assert "/tmp/t" in write and "skills-data/x" in write and "Notes" not in write and "/opt/python" not in write
    assert "UNTESTED" in (launch.macos_profile.__doc__ or "") and "UNTESTED" in (launch.__doc__ or "")


def test_seccomp_filter_is_well_formed_for_both_architectures():
    for machine, arch in (("x86_64", 0xC000003E), ("aarch64", 0xC00000B7)):
        program = lockdown.build_filter(machine, 4242)
        assert len(program) % 8 == 0 and len(program) // 8 < 4096
        records = [struct.unpack("=HBBI", program[i:i + 8]) for i in range(0, len(program), 8)]
        assert records[0] == (0x20, 0, 0, 4) and records[1] == (0x15, 1, 0, arch)  # load the architecture, compare
        assert records[2] == (0x06, 0, 0, lockdown.RET_KILL_PROCESS)  # any other architecture: the process is killed
        assert records[-1] == (0x06, 0, 0, lockdown.RET_ALLOW)
        for index, (code, jt, jf, _k) in enumerate(records):  # no jump leaves the program
            if code in (0x15, 0x35):
                assert index + 1 + max(jt, jf) < len(records)
        col = 0 if machine == "x86_64" else 1
        compared = {k for code, _jt, _jf, k in records if code == 0x15}
        assert {numbers[col] for numbers in lockdown.DENY.values()} <= compared
        assert 4242 in compared  # signals: only to this process
    numbers = [n for pair in list(lockdown.DENY.values()) + list(lockdown.OWN_PID_ONLY.values()) for n in [pair]]
    assert len({n[0] for n in numbers}) == len(numbers) and len({n[1] for n in numbers}) == len(numbers)  # no number twice
    with pytest.raises(ValueError):
        lockdown.build_filter("riscv64", 1)


def test_landlock_handles_more_rights_on_newer_kernels():
    assert lockdown.handled_fs(1) == (1 << 13) - 1
    assert lockdown.handled_fs(2) & lockdown.FS_REFER and not lockdown.handled_fs(2) & lockdown.FS_TRUNCATE
    assert lockdown.handled_fs(3) & lockdown.FS_TRUNCATE and not lockdown.handled_fs(4) & lockdown.FS_IOCTL_DEV
    assert lockdown.handled_fs(7) & lockdown.FS_IOCTL_DEV
    assert not lockdown.FS_READ & (lockdown.FS_WRITE_FILE | lockdown.FS_EXECUTE)
    assert not lockdown.FS_WRITE & (lockdown.FS_EXECUTE | lockdown.FS_MAKE_SYM | lockdown.FS_MAKE_SOCK | lockdown.FS_MAKE_CHAR
                                    | lockdown.FS_MAKE_BLOCK | lockdown.FS_MAKE_FIFO)


# ================================================================ the same Result as the in-process runner


PARITY = [
    ("done", {"q": "hi"}, [chat("GOOD answer")]),
    ("partial", {"mode": "partial"}, [chat("GOOD answer")]),
    ("embed", {"mode": "embed"}, [chat("GOOD"), (200, {"x-muse-provider": "freea"}, embeddings([[0.1, 0.2], [0.3, 0.4]]))]),
    ("stop", {"mode": "stop"}, []),
    ("crash", {"mode": "crash"}, []),
    ("needs-violation", {"mode": "bad-path"}, []),
    ("propose-outside", {"mode": "propose-outside"}, [chat("GOOD")]),
    ("bad-arguments", {"mode": "bad-args"}, []),
    ("deferred-caught", {"mode": "catch"}, [ration_429()]),
    ("deferred-with-kept", {"mode": "keep"}, [ration_429(options="wait:2026-11-14T00:00:00Z,handoff")]),
    ("privacy-refused", {"q": "x"}, [privacy_403("standard")]),
    ("router-error", {"q": "x"}, [(500, {}, b"{}")]),
    ("check-fails-then-passes-stronger", {"q": "x"}, [chat("bad"), chat("GOOD now", task="everyday")]),
    ("check-fails-twice", {"q": "x"}, [chat("bad"), chat("still bad", **{"x-muse-handoff": "suggest"})]),
    ("check-crashes", {"mode": "check-crash"}, [chat("GOOD"), chat("GOOD")]),
]


@posix
@pytest.mark.parametrize("name, inputs, script", PARITY, ids=[p[0] for p in PARITY])
def test_result_matches_the_in_process_runner(box, proxy, name, inputs, script):
    inside, sandboxed, report = both_ways(box, proxy, "polite", inputs, script)
    assert report.ran == "sandboxed" and report.separate_process and report.env_scrubbed
    assert sandboxed == inside
    assert inside["status"] in ("done", "partial", "needs_choice", "failed")


@posix
def test_budget_is_enforced_by_the_parent_and_matches(box, proxy):
    inside, sandboxed, _ = both_ways(box, proxy, "polite", {"q": "x"}, [chat("GOOD")], budget={"tokens": 20})
    assert sandboxed == inside and inside["status"] == "failed" and "budget of 20 tokens" in " ".join(inside["notes"])
    assert not proxy.requests  # nothing was sent in either run


@posix
def test_a_hostile_skill_cannot_spend_past_the_budget(box, proxy):
    out, result, report = attempt(box, "steal-budget")
    assert out["detail"] == 5 and result.used["requests"] == 5 and len(proxy.requests) == 5  # the skill's budget is 5 requests
    assert report.ran == "sandboxed"


@posix
def test_privacy_class_travels_with_every_call_and_cannot_be_loosened(box, proxy):
    proxy.push(*chat("GOOD"))
    result, _ = run(box, "polite", {"q": "x"}, privacy="private")
    assert result.status == "done" and result.output["privacy"] == "private"
    assert [r["privacy"] for r in proxy.requests] == ["private"]
    assert proxy.requests[0]["auth"].startswith("Bearer ")  # the app token is added by the parent; the child never had it


@posix
def test_proposed_writes_come_back_as_data_and_nothing_is_written(box, proxy):
    proxy.push(*chat("GOOD answer"))
    result, report = run(box, "polite", {"q": "x"}, out_dir=box.tmp / "out")
    assert result.status == "done" and [w.path for w in result.writes] == [str(box.tmp / "out" / "note.md")]
    assert result.writes[0].data == b"# Note\n\nGOOD answer\n" and not (box.tmp / "out").exists()
    assert runner.apply_writes(result)[0].applied and (box.tmp / "out" / "note.md").read_text() == "# Note\n\nGOOD answer\n"
    assert result.sandbox == report.public() and result.public()["sandbox"]["ran"] == "sandboxed"


@posix
def test_a_child_that_lies_about_a_proposed_path_is_caught_by_the_parent(box, proxy):
    target = box.outside / "planted.txt"
    out, result, _ = attempt(box, "lie-about-write", path=str(target))
    assert result.status == "failed" and not result.writes and not target.exists()
    assert "did not declare" in " ".join(result.notes) and "outside the output folder" in " ".join(result.notes)


@posix
def test_the_shipped_files_answer_skill_gives_the_same_result_sandboxed(env, proxy, tmp_path):  # noqa: F811
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.md").write_text("# Lease\n\nThe flat is at 12 Harbour Street.\n\n"
                                   "The monthly rent is 18,500 dollars, due on the 5th of each month.\n", encoding="utf-8")
    (docs / "garden.txt").write_text("Tomatoes need six hours of sun.\n\nWater the basil every morning.\n", encoding="utf-8")
    answer = json.dumps({"answer": "The monthly rent is 18,500 dollars [1].",
                         "claims": [{"quote": "The monthly rent is 18,500 dollars", "n": 1,
                                     "says": "The monthly rent is 18,500 dollars"}], "not_found": False})
    results = []
    for mode in ("off", "require" if caps().can_require else "auto"):
        shutil.rmtree(Path(env["MUSE_HOME"]) / "skills-data", ignore_errors=True)
        proxy.push(*chat(answer))
        result, report = sandbox.run_skill("files-answer", {"question": "How much is the monthly rent?", "embeddings": "off"},
                                           roots=[docs], mode=mode, environ=env)
        results.append(plain(result))
    assert results[0]["status"] == "done" and "18,500" in results[0]["output"]["text"]
    assert results[1] == results[0]
    assert report.ran == "sandboxed"
    assert len(proxy.requests) == 2 and proxy.requests[0]["messages"] == proxy.requests[1]["messages"]  # the same prompt left the home


@posix
def test_the_shipped_research_report_recovers_empty_answers_the_same_way_sandboxed(env, proxy, tmp_path):  # noqa: F811
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "lease.md").write_text("# Lease\n\nThe monthly rent is 18,500 dollars, due on the 5th of each month.\n\n"
                                   "The lease ends on 30 June 2032 and needs 60 days of notice.\n", encoding="utf-8")
    quotes = ["The monthly rent is 18,500 dollars", "The lease ends on 30 June 2032"]

    def respond(request: dict):
        if request["path"].endswith("/embeddings"):
            return None
        system, user = request["messages"][0]["content"], request["messages"][1]["content"]
        if system.startswith("You plan"):
            return chat(json.dumps({"subquestions": ["What is the monthly rent?", "When does the lease end?"]}))
        if system.startswith("You read"):
            if request["task"] == "everyday":  # every first try is empty: the reasoning took the whole allowance
                return chat("", **{"x-muse-answer": "empty-reasoning"})
            given = re.findall(r"^\[(\d+)\] (.*?)(?=^\[\d+\] |\Z)", user.split("\n\nQuestion:")[0], flags=re.S | re.M)
            claims = [{"quote": q, "n": int(n), "says": q} for n, text in given for q in quotes if q in text]
            return chat(json.dumps({"claims": claims}))
        ids = re.findall(r"^- (.*) \[(\d+)\]$", user, re.M)
        return chat("## Lease\n" + " ".join(f"{text} [{n}]." for text, n in ids))

    proxy.responder = respond
    results = []
    for mode in ("off", "require" if caps().can_require else "auto"):
        shutil.rmtree(Path(env["MUSE_HOME"]) / "skills-data", ignore_errors=True)
        proxy.requests.clear()
        result, report = sandbox.run_skill("research-report", {"question": "Rent and end date of the lease?", "embeddings": "off"},
                                           roots=[docs], mode=mode, environ=env)
        results.append(plain(result))
    assert results[0]["status"] == "done", results[0]["check"]
    assert results[0]["output"]["usage"]["extract"]["recovered"] >= 1 and "18,500" in results[0]["output"]["text"]
    assert results[1] == results[0] and report.ran == "sandboxed"


@posix
def test_events_from_the_child_are_only_skill_facts(box, proxy):
    out, result, _ = attempt(box, "forge-event")
    kinds = [e["kind"] for e in result.events]
    assert kinds.count("end") == 1 and kinds[-1] == "end" and "call" not in kinds  # the forged `end` and `call` were dropped
    assert "skill.ok" in kinds and result.status == "done"


@posix
def test_file_content_cannot_ride_out_in_an_error(box, proxy):
    (box.docs / "secret.txt").write_text(CANARY_OUTSIDE, encoding="utf-8")
    out, result, _ = attempt(box, "traceback", path=str(box.docs / "secret.txt"))
    assert result.status == "failed" and len(result.notes) == 1
    assert re.fullmatch(r"The skill stopped with an error \(RuntimeError at skill\.py:\d+\)\.", result.notes[0])
    assert CANARY_OUTSIDE not in json.dumps(result.public(), default=str)


# ================================================================ no inherited secrets


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_the_child_sees_no_secret_from_the_parent_environment(box, proxy, layer):
    out, result, report = attempt(box, "environ", layer)
    child = out["detail"]
    assert set(child) <= {"PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TZ", "MUSE_TZ", "LC_CTYPE", "PWD"}  # PWD: set by bubblewrap
    assert child.get("PWD", child["TMPDIR"]) == child["TMPDIR"]
    assert child["PATH"] == "/usr/bin:/bin" and child["MUSE_TZ"] == "UTC"
    assert CANARY_ENV not in json.dumps(result.public(), default=str)
    assert os.environ["MUSE_CANARY_SECRET"] == CANARY_ENV and "MUSE_HOME" not in child and report.env_scrubbed


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_only_stdio_and_the_control_pipe_are_open_in_the_child(box, proxy, layer):
    leak = os.open(str(box.home / "canary.txt"), os.O_RDONLY)
    os.set_inheritable(leak, True)
    try:
        out, result, _ = attempt(box, "fds", layer)
    finally:
        os.close(leak)
    fds = out["detail"]
    assert fds[:3] == [0, 1, 2] and len(fds) == 5, fds  # stdio and the two ends of the control pipe: nothing else


@posix
def test_the_child_runs_from_a_private_temp_home(box, proxy):
    out, result, report = attempt(box, "python", "all")
    facts = out["detail"]
    assert facts["home"].endswith("/home") and str(box.home) not in facts["home"] and facts["cwd"] != os.getcwd()
    if not report.bwrap:
        assert not os.path.exists(os.path.dirname(facts["home"]))  # the parent removed the temp folder afterwards


# ================================================================ limits, timeouts, the kill


def limited(layer="all", **changes) -> Policy:
    return dataclasses.replace(LAYERS[layer], **changes)


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_memory_is_capped(box, proxy, layer):
    out, result, report = attempt(box, "memory", layer, bytes=1 << 30, policy=limited(layer, memory_bytes=400 << 20))
    assert out == {"escaped": False, "error": "MemoryError", "detail": None} and report.rlimits["AS"] == 400 << 20


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_one_file_cannot_grow_past_the_cap(box, proxy, layer):
    out, result, _ = attempt(box, "big-file", layer, bytes=4 << 20, policy=limited(layer, file_bytes=1 << 20))
    assert out["escaped"] is False and out["error"] == "OSError" and out["detail"] == 27  # EFBIG
    assert (box.data / "big.bin").stat().st_size <= 1 << 20


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_filling_the_data_folder_past_the_cap_ends_the_run(box, proxy, layer):
    policy = limited(layer, data_bytes=2 << 20, watch_seconds=0.05)
    out, result, report = attempt(box, "fill", layer, files=3, size=1 << 20, policy=policy)  # quick: over the cap on return
    assert result.status == "failed" and report.stopped == "data-cap" and "past the allowed size" in result.notes[-1]
    shutil.rmtree(box.data)
    started = time.monotonic()
    out, result, report = attempt(box, "fill", layer, files=100_000, size=256 << 10, policy=policy)  # endless: ended while writing
    assert result.status == "failed" and report.stopped == "data-cap" and time.monotonic() - started < 15
    written = sum(f.stat().st_size for f in box.data.iterdir())
    assert written < 1 << 30, written  # stopped long before the 25 GB it asked for
    assert [e["reason"] for e in result.events if e["kind"] == "sandbox.stop"] == ["data-cap"]


@posix
def test_filling_the_private_temp_folder_ends_the_run_when_it_is_not_a_tmpfs(box, proxy):
    policy = limited("none", tmp_bytes=2 << 20, watch_seconds=0.05)
    out, result, report = attempt(box, "fill-tmp", "none", files=100_000, size=256 << 10, policy=policy)
    assert result.status == "failed" and report.stopped == "data-cap"


@posix
@pytest.mark.parametrize("layer", ["none", "all"])
def test_a_spinning_skill_is_ended_at_its_time_budget(box, proxy, layer):
    started = time.monotonic()
    out, result, report = attempt(box, "spin", layer, budget={"seconds": 1}, policy=limited(layer, grace_seconds=1))
    took = time.monotonic() - started
    assert result.status == "failed" and report.stopped == "timeout" and 1.5 < took < 8
    assert result.notes == ["Stopped: the skill ran past its time budget and was ended."] and result.output is None


@posix
def test_processor_time_is_capped_separately_from_wall_time(box, proxy):
    out, result, report = attempt(box, "spin", "none", policy=limited("none", cpu_seconds=1))
    assert result.status == "failed" and report.stopped == "cpu" and "processor time" in result.notes[0]


@posix
@pytest.mark.parametrize("layer", ["bwrap", "seccomp", "all"])
def test_everything_the_skill_started_dies_with_it(box, proxy, layer):
    need(layer)
    out, result, report = attempt(box, "orphan", layer, budget={"seconds": 1}, policy=limited(layer, grace_seconds=1))
    assert result.status == "failed" and report.stopped == "timeout"
    beat = box.data / "heartbeat"
    assert beat.exists()  # the grandchild did run
    first = beat.read_text()
    time.sleep(0.6)
    assert beat.read_text() == first, "a process the skill left behind is still writing"


@posix
def test_fork_bomb_is_stopped_by_the_process_limit(box, proxy):
    if os.geteuid() == 0:
        pytest.skip("running as root: the kernel does not apply RLIMIT_NPROC to root, so this limit cannot be shown here")
    out, result, report = attempt(box, "fork", "all", count=5000, policy=limited("all", processes=32))
    assert report.nproc_enforced and (out["escaped"] is False or out["detail"] < 5000)


# ================================================================ a child that breaks the conversation


@posix
@pytest.mark.parametrize("what, stopped", [("garbage", "protocol"), ("huge-frame", "protocol"), ("exit", "crashed")])
def test_garbage_oversized_frames_and_early_exit_fail_plainly(box, proxy, what, stopped):
    out, result, report = attempt(box, what, "all")
    assert result.status == "failed" and result.output is None and report.stopped == stopped
    assert result.notes == [sandbox_host.STOP_WORDS[stopped]] and result.events[-1] == {**result.events[-1], "kind": "end", "status": "failed"}
    assert "Traceback" not in json.dumps(result.public(), default=str)


@posix
def test_a_reply_larger_than_the_cap_is_refused(box, proxy):
    proxy.push(*chat("GOOD"))
    result, report = run(box, "polite", {"q": "x" * 5000}, policy=Policy(frame_bytes=2048))  # the inputs come back in the output
    assert result.status == "failed" and report.stopped == "protocol"


class FakeProc:
    pid = 2**22 + 12345  # no such process group

    def poll(self):
        return 0

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass


def scripted_executor(frames: list[bytes]) -> tuple[sandbox_host.Executor, object]:
    """An executor whose 'child' is a pipe already holding `frames`: the parent's handling, with no process at all."""
    r, w = os.pipe()
    back_r, back_w = os.pipe()
    for frame in frames:
        os.write(w, frame)
    os.close(w)
    executor = sandbox_host.Executor(Policy(), "auto")
    executor._chan = protocol.Channel(r, back_w, 1 << 16)
    executor._proc = FakeProc()  # type: ignore[assignment]
    executor._deadline = time.monotonic() + 2
    executor._skill = loader.find("polite", {"MUSE_SKILLS_DIR": str(FIXTURES), "MUSE_HOME": os.environ["MUSE_HOME"]})
    workspace = runner.Workspace([], None)
    return executor, workspace


def frame(**fields) -> bytes:
    return protocol.encode(fields)


DONE = {"op": "attempt_done", "n": 1, "ok": True, "output": {"text": "x"}, "notes": [], "incomplete": False, "proposed": [], "skipped_links": 0}


@pytest.mark.parametrize("frames, code", [
    ([frame(op="surprise")], "protocol"),                                       # an operation that does not exist
    ([frame(op="check_done", reasons=[])], "protocol"),                         # the right kind of frame at the wrong time
    ([frame(op="reply", ok=1)], "protocol"),                                    # the child may not send replies
    ([frame(op="ask", messages="not a list", handoff=False)], "protocol"),      # a request with the wrong shape
    ([frame(op="ask", messages=[{"role": "user"}], handoff="yes")], "protocol"),
    ([frame(op="embed", texts={"a": 1})], "protocol"),
    ([frame(**{**DONE, "n": 2})], "protocol"),                                  # an answer for another attempt
    ([frame(**{**DONE, "notes": "one long string"})], "protocol"),
    ([frame(**{**DONE, "proposed": [{"path": 5}]})], "protocol"),
    ([frame(**{**DONE, "proposed": [{"path": "a", "data": "@@not base64@@"}]})], "protocol"),
    ([frame(**{**DONE, "proposed": [{"path": "a", "data": ""}] * 500})], "protocol"),
    ([frame(**{**DONE, "ok": False})], "protocol"),                             # failed, but no error given
    ([b"\x00\x00"], "crashed"),                                                 # the pipe closes in the middle of a header
    ([], "crashed"),
])
def test_the_parent_refuses_frames_out_of_turn_or_out_of_shape(frames, code):
    executor, workspace = scripted_executor(frames)
    client = type("Client", (), {"default_task": "light", "raise_steps": 0, "allow_paid": False, "remaining": lambda self: {}})()
    ctx, output, exc = executor.attempt(1, {}, client, workspace, "standard", lambda kind, **facts: None)
    assert output is None and isinstance(exc, sandbox_host.SandboxStop) and exc.code == code and executor.report.stopped == code
    assert str(exc) == sandbox_host.STOP_WORDS[code] and isinstance(exc, runner.SkillStop)
    again = executor.attempt(2, {}, client, workspace, "standard", lambda kind, **facts: None)
    assert again[2] is exc  # once ended, it stays ended


def test_errors_reported_by_the_child_are_never_trusted_as_text():
    def one(error):
        executor, workspace = scripted_executor([frame(**{**DONE, "ok": False, "error": error})])
        client = type("Client", (), {"default_task": "light", "raise_steps": 0, "allow_paid": False})()
        return executor.attempt(1, {}, client, workspace, "standard", lambda kind, **facts: None)[2]

    forged = one({"kind": "llm", "id": 99})  # an id the parent never issued: not a budget stop, not a deferral
    assert type(forged).__name__ == "LlmError" and not isinstance(forged, sandbox_host.LlmError)
    nasty = one({"kind": "error", "type": "x" * 500 + "\n<script>", "where": "../../etc/passwd:1\nsecond line"})
    assert type(nasty).__name__ == "Exception" and nasty.sandbox_where == "" and str(nasty) == ""
    fine = one({"kind": "error", "type": "ZeroDivisionError", "where": "helper.py:5"})
    assert type(fine).__name__ == "ZeroDivisionError" and runner._where(fine) == "helper.py:5"
    stop = one({"kind": "stop", "message": "line one\nline two " + "x" * 1000})
    assert isinstance(stop, runner.SkillStop) and "\n" not in str(stop) and len(str(stop)) == 300


def test_events_from_the_child_are_filtered_and_capped():
    seen = []
    frames = [frame(op="event", kind="skill.ok", facts={"n": 1, "options": ["text smuggled as options"]}),
              frame(op="event", kind="end", facts={"status": "done"}), frame(op="event", kind="skill." + "x" * 80, facts={}),
              frame(op="event", kind="skill.ok", facts="not a mapping"), frame(op="event", kind=5, facts={})]
    frames += [frame(op="event", kind="skill.flood", facts={"i": i}) for i in range(50)] + [frame(**DONE)]
    executor, workspace = scripted_executor(frames)
    executor.policy = Policy(events=20)
    client = type("Client", (), {"default_task": "light", "raise_steps": 0, "allow_paid": False})()
    ctx, output, exc = executor.attempt(1, {}, client, workspace, "standard", lambda kind, **facts: seen.append((kind, facts)))
    assert exc is None and output == {"text": "x"}
    assert seen[0] == ("skill.ok", {"n": 1}) and {kind for kind, _ in seen} == {"skill.ok", "skill.flood"} and len(seen) == 16


# ================================================================ modes, refusals, the rule for skills of your own


@posix
def test_require_refuses_when_the_layers_are_missing(box, proxy):
    result, report = run(box, "polite", {"q": "x"}, "none", mode="require")
    assert result.status == "failed" and report.ran == "refused" and not report.isolated
    assert "only inside the sandbox" in result.notes[0] and "muse doctor" in result.notes[0]
    assert not proxy.requests and not (box.home / "skills-data" / "polite").exists()  # nothing ran, nothing was created
    assert [e["kind"] for e in result.events] == ["start", "end"]


@posix
def test_require_refuses_on_a_machine_with_no_layers_at_all(box, proxy, monkeypatch):
    bare = Capabilities(platform="linux", machine="x86_64", bwrap_reason="bubblewrap is not installed")
    monkeypatch.setattr(sandbox_host, "probe", lambda: bare)
    result, report = run(box, "polite", {"q": "x"}, "all", mode="require")
    assert result.status == "failed" and report.ran == "refused"
    assert "needs bubblewrap" in result.notes[0]
    proxy.push(*chat("GOOD"))
    result, report = run(box, "polite", {"q": "x"}, "all", mode="auto")  # auto still runs, and says how little it had
    assert result.status == "done" and report.ran == "sandboxed" and not report.isolated
    assert report.files == "not isolated" and report.net == "not blocked" and report.env_scrubbed and report.rlimits
    assert "NOT fully isolated" in report.sentence() and any("bubblewrap is not used" in note for note in report.best_effort)


@posix
def test_auto_reports_exactly_the_layers_that_were_active(box, proxy):
    c = caps()
    proxy.responder = lambda request: chat("GOOD")
    for layer in ("none", "bwrap", "landlock", "seccomp", "all"):
        result, report = run(box, "polite", {"q": "x"}, layer)
        assert result.status == "done", (layer, result.notes)
        want_bwrap = c.bwrap_works and layer in ("bwrap", "all")
        want_landlock = c.landlock_works and layer in ("landlock", "all")
        want_seccomp = c.seccomp_works and layer in ("seccomp", "all")
        assert (report.bwrap, bool(report.landlock_abi), report.seccomp) == (want_bwrap, want_landlock, want_seccomp), layer
        assert report.files == ("isolated" if want_bwrap or (want_landlock and c.landlock_abi >= 3) else
                                "best effort" if want_landlock else "not isolated")
        assert report.net == ("blocked" if want_bwrap or want_seccomp else
                              "tcp blocked" if want_landlock and c.landlock_abi >= 4 else "not blocked")
        assert report.isolated == (report.files == "isolated" and report.net == "blocked")
        assert report.no_new_privs is LINUX and report.rlimits
        sandbox_event = next(e for e in result.events if e["kind"] == "sandbox")
        assert sandbox_event["isolated"] == report.isolated and sandbox_event["net"] == report.net


@posix
def test_a_skill_of_your_own_always_runs_with_require(box, proxy, monkeypatch):
    real_home_skills(monkeypatch)
    # This test is about the sandbox. A skill of your own is also always checked against permissions (`enforce`,
    # muse/compiler/guard.py), which would refuse it here for want of one; tests/test_compiler_guard.py covers that.
    from muse.compiler import guard as guard_mod

    monkeypatch.setattr(guard_mod, "mode_for", lambda source, env: "off")
    write_skill(box.home, "mine")
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    proxy.responder = lambda request: chat("GOOD")
    for asked, setting in ((None, None), ("off", "off"), ("auto", "auto")):
        env_run = dict(env_, **({"MUSE_SANDBOX": setting} if setting else {}))
        result = runner.run("mine", {"q": "hi"}, environ=env_run, sandbox=asked)
        assert result.sandbox is not None and result.sandbox["mode"] == "require", (asked, setting)
        if caps().can_require:
            assert result.status == "done" and result.sandbox["isolated"] is True
        else:
            assert result.status == "failed" and result.sandbox["ran"] == "refused"


def test_a_skill_that_never_started_is_not_reported_as_having_run(box, proxy, monkeypatch):
    real_home_skills(monkeypatch)
    write_skill(box.home, "unread", approve=False)
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    result = runner.run("unread", {"q": "hi"}, environ=env_)
    assert result.status == "failed" and "not approved yet" in result.notes[0] and result.sandbox is None
    result, report = sandbox.run_skill("unread", {"q": "hi"}, environ=env_)
    assert result.status == "failed" and "not approved yet" in result.notes[0]
    assert report.ran == "refused" and not report.isolated and "not approved" in report.reason and result.sandbox == report.public()


@posix
def test_a_skill_of_your_own_is_refused_where_the_sandbox_is_incomplete(box, proxy, monkeypatch):
    real_home_skills(monkeypatch)
    write_skill(box.home, "mine")
    monkeypatch.setattr(sandbox_host, "probe", lambda: Capabilities(platform="linux", landlock_works=True, landlock_abi=7))
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    result = runner.run("mine", {"q": "hi"}, environ=env_, sandbox="off")
    assert result.status == "failed" and not proxy.requests
    assert result.notes == ["skill 'mine' is not one of Muse's own, so it runs only inside the sandbox, and the sandbox is not complete "
                            "here: nothing here can cut a skill off from the network (needs bubblewrap, or seccomp). See `muse doctor`."]


def test_with_the_test_hatch_a_home_skill_still_runs_in_process_by_default(box, proxy):
    write_skill(box.home, "mine")
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    proxy.push(*chat("GOOD"))
    result = runner.run("mine", {"q": "hi"}, environ=env_)
    assert result.status == "done" and result.sandbox is None and "sandbox" not in result.public()


def test_shipped_skills_follow_the_setting_and_default_to_off(box, proxy):
    proxy.responder = lambda request: chat("GOOD")
    assert runner.run("polite", {"q": "x"}, folders=[box.docs], environ=box.env).sandbox is None
    result = runner.run("polite", {"q": "x"}, folders=[box.docs], environ=dict(box.env, MUSE_SANDBOX="off"))
    assert result.status == "done" and result.sandbox is None
    result = runner.run("polite", {"q": "x"}, folders=[box.docs], environ=dict(box.env, MUSE_SANDBOX="bogus"))
    assert result.status == "failed" and "off, auto, require" in result.notes[0]
    if sys.platform != "win32":
        result = runner.run("polite", {"q": "x"}, folders=[box.docs], environ=dict(box.env, MUSE_SANDBOX="auto"))
        assert result.status == "done" and result.sandbox["ran"] == "sandboxed" and result.sandbox["mode"] == "auto"


def test_shipped_skills_that_read_muse_state_stay_in_process_under_auto_and_are_refused_under_require(env, proxy):  # noqa: F811
    names = {name: sandbox_host.in_process_reason(loader.find(name, env)) for name in
             ("files-answer", "research-report", "recurring-chore", "setup-services")}
    assert not names["files-answer"] and not names["research-report"]
    assert "Muse's own state" in names["recurring-chore"] and "Muse's own state" in names["setup-services"]
    result, report = sandbox.run_skill("setup-services", {}, mode="auto", environ=env)
    assert report.ran == "in-process" and "Muse's own state" in report.reason and result.sandbox["isolated"] is False
    assert "Ran without isolation (this shipped skill" in report.sentence()
    result, report = sandbox.run_skill("setup-services", {}, mode="require", environ=env)
    assert result.status == "failed" and report.ran == "refused" and "which the sandbox hides" in result.notes[0]


def test_a_skill_that_asks_for_hosts_is_still_refused(box, proxy, monkeypatch):
    real_home_skills(monkeypatch)
    write_skill(box.home, "netty", muse_needs={"files.read": [], "files.write": [], "net": ["example.com"]})
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    result = runner.run("netty", {}, environ=env_, sandbox="require")
    assert result.status == "failed" and "internet access of its own" in result.notes[0] and result.sandbox is None


@posix
def test_a_folder_that_contains_muse_home_is_refused(box, proxy):
    result, report = run(box, "polite", {"q": "x"}, roots=[box.home.parent])
    assert result.status == "failed" and report.ran == "refused" and "name a narrower folder" in result.notes[0]


@posix
def test_unknown_skill_fails_the_same_way_as_the_runner(box, proxy):
    result, report = sandbox.run_skill("no-such-skill", {}, environ=box.env)
    assert result.status == "failed" and "no skill named 'no-such-skill'" in result.notes[0] and report.ran == "refused"


@posix
def test_a_skill_that_cannot_be_imported_is_reported_by_type_only(box, proxy, monkeypatch):
    real_home_skills(monkeypatch)
    if not caps().can_require:
        pytest.skip("no complete sandbox here: " + "; ".join(caps().missing()))
    write_skill(box.home, "broken", code=f"raise RuntimeError('{CANARY_OUTSIDE}')\n")
    env_ = {k: v for k, v in box.env.items() if k != "MUSE_SKILLS_DIR"}
    result = runner.run("broken", {}, environ=env_)
    assert result.status == "failed" and result.notes == ["skill 'broken' could not be imported (RuntimeError)"]


# ================================================================ the permission hook


@posix
def test_the_token_is_handed_to_the_authorize_hook_and_a_refusal_stops_the_call(box, proxy):
    seen = []

    def allow(action, facts, token):
        seen.append((action, dict(facts), token))

    proxy.push(*chat("GOOD"))
    result, _ = run(box, "polite", {"q": "x"}, policy=Policy(authorize=allow), token="token-abc")
    assert result.status == "done"
    assert [(action, token) for action, _facts, token in seen] == [("llm.ask", "token-abc"), ("files.propose", "token-abc")]
    assert seen[0][1] == {"task": None, "privacy": None, "messages": 1}  # facts, never the prompt

    def deny(action, facts, token):
        raise PermissionError("this permission does not cover model calls")

    result, _ = run(box, "polite", {"q": "x"}, policy=Policy(authorize=deny), token="token-abc")
    assert result.status == "failed" and len(proxy.requests) == 1  # only the first run's call was sent
    assert re.fullmatch(r"The skill stopped with an error \(PermissionError at \w+\.py:\d+\)\.", result.notes[0])


# ================================================================ command line and doctor


def run_cli(capsys, *argv: str) -> tuple[int, str, str]:
    code = cli.main(list(argv))
    captured = capsys.readouterr()
    return code, captured.out, captured.err


@posix
def test_skills_run_with_the_sandbox_flag_says_how_it_ran(box, proxy, capsys):
    proxy.responder = lambda request: chat("GOOD answer")
    code, out, err = run_cli(capsys, "skills", "run", "polite", "--folder", str(box.docs), "--sandbox", "auto")
    assert code == 0 and "GOOD answer" in out
    line = next(line for line in err.splitlines() if line.startswith("Ran "))
    assert line.startswith("Ran isolated: ") if caps().can_require else "NOT fully isolated" in line
    code, out, err = run_cli(capsys, "skills", "run", "polite", "--folder", str(box.docs), "--json", "--sandbox", "auto")
    assert json.loads(out)["sandbox"]["ran"] == "sandboxed"
    code, out, err = run_cli(capsys, "skills", "run", "polite", "--folder", str(box.docs))
    assert code == 0 and "Ran " not in err  # the sandbox was not involved: nothing is claimed either way


def test_doctor_reports_the_sandbox(env, capsys):  # noqa: F811
    code, out, _ = run_cli(capsys, "doctor", "--json")
    check = next(c for c in json.loads(out)["checks"] if c["id"] == "sandbox")
    c = caps()
    assert check["status"] == ("ok" if c.can_require else "warn")
    assert "platform:" in check["detail"] and "MUSE_SANDBOX=off" in check["detail"]
    if c.can_require:
        assert "can isolate files and network" in check["title"]
    else:
        assert "will be refused" in check["title"] and check["fix"]
