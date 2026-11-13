"""The Muse tools server over real stdio: a child process, the official SDK's client, the fake home.

    test process ──(SDK client, stdin/stdout)──> python -m muse.mcp ──(home control API, Unix socket)──> fake home (in this process)

No network and no provider. The person's answers to cards are given from the
test through the home API, as a phone would. Two canaries run through every
test: the server's device token and a prompt; neither may appear in any tool
result or on the server's stderr.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("mcp", reason="the official MCP SDK is not installed (pip install 'muse-stack[mcp]')")

import anyio  # noqa: E402
from mcp import ClientSession, StdioServerParameters, stdio_client  # noqa: E402

from home_fakes import ROOT, client_for, running  # noqa: E402
from muse.home import fake  # noqa: E402
from muse.home.auth import StaticVerifier  # noqa: E402
from muse.mcp import SCOPE_NOTE, tools  # noqa: E402

HOST, HOST_TOKEN = "host-tools", "sk-muse-dev-CANARYstdioTOKEN0123456789abcdefXYZ"
PHONE, PHONE_TOKEN = "my-phone", "phone-device-token-for-tests"
PROMPT_CANARY = "CANARY-PROMPT-77aa summarise my private notes"
KEY_CANARY = "gsk_CANARYstdio0123456789abcdefABCDEF"
ASK = {"kind": "save_file", "title": "Save a file", "thing": "notes.md", "where": "Documents"}

LOG_LINE = re.compile(r"^muse-mcp (INFO|WARNING|ERROR) (started tools=\d+ token=(file|env)|tool=[a-z_()]+ outcome=[a-z_]+ ms=\d+)$")


class Rig:
    """A fake home on a socket, and a way to run one client session against a fresh server process."""

    def __init__(self, tmp_path: Path, state: str = "normal") -> None:
        self.tmp = tmp_path
        self.home = fake.FakeHome(state)
        self._ctx = running(self.home, StaticVerifier({HOST: HOST_TOKEN, PHONE: PHONE_TOKEN}))
        self.srv = self._ctx.__enter__()
        self.phone = client_for(self.srv, PHONE_TOKEN)
        self.token_file = tmp_path / "host.token"
        self.token_file.write_text(HOST_TOKEN + "\n", encoding="utf-8")
        self.token_file.chmod(0o600)
        self.results: list[str] = []
        self.stderr = ""

    def close(self) -> None:
        self._ctx.__exit__(None, None, None)
        self.home.close()

    def env(self, **more: str) -> dict[str, str]:
        return {"PYTHONPATH": str(ROOT), "MUSE_HOME": str(self.tmp / "unused-home"), **more}

    def args(self, *more: str, socket: Path | str | None = None) -> list[str]:
        return ["--token-file", str(self.token_file), "--socket", str(socket or self.srv.socket_path), *more]

    def session(self, fn, *, args: list[str] | None = None, env: dict[str, str] | None = None, module: list[str] | None = None):
        """Start the server, initialize, run `await fn(call, session, init)`, stop it. Returns fn's result."""
        errlog = self.tmp / f"stderr-{time.monotonic_ns()}.log"

        async def call(name: str, arguments: dict | None = None):
            result = await session_box[0].call_tool(name, arguments)
            assert len(result.content) == 1 and result.content[0].type == "text"
            self.results.append(result.content[0].text)
            assert json.loads(result.content[0].text) == result.structured_content  # the text is the same data
            return result.structured_content, result.is_error

        session_box: list[ClientSession] = []

        async def main():
            with open(errlog, "w+", encoding="utf-8") as err:
                params = StdioServerParameters(command=sys.executable, args=[*(module or ["-m", "muse.mcp"]), *(args or self.args())],
                                               env=env or self.env(), cwd=str(self.tmp))
                with anyio.fail_after(90):
                    async with stdio_client(params, errlog=err) as (read, write):
                        async with ClientSession(read, write) as session:
                            init = await session.initialize()
                            session_box.append(session)
                            return await fn(call, session, init)

        try:
            return anyio.run(main)
        finally:
            if errlog.exists():
                self.stderr += errlog.read_text(encoding="utf-8")

    def pending(self) -> list[dict]:
        return [c for c in self.home.cards.list("pending") if c["job_ref"] == f"device:{HOST}"]

    async def answer_next(self, decision: str) -> str:
        """Wait for the host's card to appear, then answer it from the phone."""
        with anyio.fail_after(15):
            while not self.pending():
                await anyio.sleep(0.05)
        card_id = self.pending()[0]["id"]
        status, _ = await anyio.to_thread.run_sync(self.phone.call, "POST", f"/v1/cards/{card_id}/answer", {"decision": decision})
        assert status == 200
        return card_id


@pytest.fixture()
def start(tmp_path):
    made: list[Rig] = []

    def make(state: str = "normal") -> Rig:
        made.append(Rig(tmp_path, state))
        return made[-1]

    yield make
    for r in made:
        r.close()
        everything = "".join(r.results) + r.stderr
        assert HOST_TOKEN not in everything and "CANARY" not in everything
        assert "Traceback" not in r.stderr
        for line in r.stderr.splitlines():  # the log is types only: which tool, how it ended, how long
            assert LOG_LINE.fullmatch(line), line


@pytest.fixture()
def rig(start):
    return start()


# ---------------------------------------------------------------- lifecycle and the tool list


def test_initialize_and_list_tools(rig):
    async def go(call, session, init):
        return init, await session.list_tools()

    init, listed = rig.session(go)
    assert init.server_info.name == "muse" and init.server_info.version
    assert init.capabilities.tools is not None
    assert SCOPE_NOTE in init.instructions
    assert [t.name for t in listed.tools] == [t.name for t in tools.TOOLS]
    by_name = {t.name: t for t in listed.tools}
    for spec in tools.TOOLS:
        got = by_name[spec.name]
        assert got.description == spec.description and got.input_schema == spec.input_schema
        assert got.annotations.read_only_hint is spec.read_only and got.annotations.destructive_hint is False
    for absent in ("pay", "pause", "resume"):
        assert absent not in by_name
    assert "started tools=11 token=file" in rig.stderr


def test_every_tool_answers_over_stdio(rig):
    async def go(call, session, init):
        out = {}
        out["allowance"] = await call("allowance_status")
        out["pause"] = await call("pause_status", {})
        out["suggest"] = await call("suggest_service")
        out["permission"] = await call("check_permission", {"action": "write", "resource": "/home/me/a.txt"})
        out["record"] = await call("record_action", {"action": "saved a file", "summary": "Wrote notes.md", "outcome": "done"})
        out["job"] = await call("queue_job", {"prompt": PROMPT_CANARY, "task": "light"})
        out["job_status"] = await call("job_status", {"id": out["job"][0]["job"]["id"]})
        out["skills"] = await call("list_skills")
        out["run_skill"] = await call("run_skill", {"name": "weekly-digest"})
        out["ask"] = await call("ask_approval", {**ASK, "timeout_s": 0})
        out["check"] = await call("check_approval", {"card_id": out["ask"][0]["card_id"]})
        return out

    out = rig.session(go)
    assert all(is_error is False for _, is_error in out.values()), out
    got = {name: result for name, (result, _) in out.items()}
    assert got["allowance"]["paused"] is False and got["allowance"]["services"] and got["allowance"]["paid_today"]["cap_usd"] == 1.0
    assert got["pause"]["paused"] is False and got["pause"]["scope_note"] == SCOPE_NOTE
    assert got["suggest"]["suggestion"]["id"]
    assert got["permission"]["decision"] == "ask" and got["permission"]["scope_note"] == SCOPE_NOTE
    assert got["record"]["recorded"] is False and got["record"]["reason"] == "records are not built yet"
    assert got["job"]["job"]["status"] == "queued" and got["job_status"]["job"]["id"] == got["job"]["job"]["id"]
    assert rig.home.queue().get(got["job"]["job"]["id"]).payload == PROMPT_CANARY  # it arrived; it never comes back
    assert got["skills"]["built"] is False and got["run_skill"]["ran"] is False
    assert got["ask"]["decision"] == "pending" and got["check"]["decision"] == "pending"
    assert got["ask"]["scope_note"] == got["check"]["scope_note"] == SCOPE_NOTE
    for name in [t.name for t in tools.TOOLS]:
        assert f"tool={name} outcome=ok" in rig.stderr


def test_muse_mcp_serve_is_the_same_server(rig):
    async def go(call, session, init):
        return init.server_info.name, [t.name for t in (await session.list_tools()).tools], await call("pause_status")

    name, names, (pause, is_error) = rig.session(go, module=["-m", "muse.cli", "mcp", "serve"], env=rig.env(MUSE_ENV_FILE=str(rig.tmp / "none.env")))
    assert name == "muse" and names == [t.name for t in tools.TOOLS] and pause["paused"] is False and not is_error


def test_the_token_can_come_from_the_environment_instead(rig):
    async def go(call, session, init):
        return await call("pause_status")

    result, is_error = rig.session(go, args=["--socket", str(rig.srv.socket_path)], env=rig.env(MUSE_HOME_TOKEN=HOST_TOKEN))
    assert not is_error and result["paused"] is False and "token=env" in rig.stderr


def test_allow_read_is_given_when_the_server_starts(rig):
    async def go(call, session, init):
        return [await call("check_permission", {"action": a, "resource": r}) for a, r in (
            ("read", "/home/me/notes/a.md"), ("read", "/home/me/other/a.md"), ("write", "/home/me/notes/a.md"), ("read", "/srv/docs/x"))]

    got = rig.session(go, args=rig.args("--allow-read", "/home/me/notes"), env=rig.env(MUSE_MCP_ALLOW_READ="/srv/docs"))
    assert [r["decision"] for r, _ in got] == ["allow", "ask", "ask", "allow"]


# ---------------------------------------------------------------- approvals, answered by the person through the home


def test_ask_approval_approved_and_declined(rig):
    async def go(call, session, init):
        out = {}
        for decision in ("approve", "decline"):
            async with anyio.create_task_group() as tg:
                async def ask(key=decision):
                    out[key] = await call("ask_approval", {**ASK, "thing": f"{key}.md", "timeout_s": 30})

                tg.start_soon(ask)
                out[decision + "_id"] = await rig.answer_next(decision)
        return out

    started = time.monotonic()
    out = rig.session(go)
    assert time.monotonic() - started < 25  # it returned when the person answered, not when the wait ran out
    approved, declined = out["approve"][0], out["decline"][0]
    assert approved["decision"] == "approved" and approved["card_id"] == out["approve_id"] and approved["scope_note"] == SCOPE_NOTE
    assert declined["decision"] == "declined" and "Do not do this action" in declined["what_to_do"]
    assert out["approve"][1] is False and out["decline"][1] is False
    card = rig.home.cards.get(out["approve_id"])
    assert card["thing"] == "approve.md" and card["answered_by"] == PHONE and card["job_ref"] == f"device:{HOST}"


def test_ask_approval_expired(rig):
    async def go(call, session, init):
        return await call("ask_approval", {**ASK, "timeout_s": 20, "valid_for_s": 1})

    result, is_error = rig.session(go)
    assert not is_error and result["decision"] == "expired"
    assert rig.home.cards.get(result["card_id"])["status"] == "expired"


def test_ask_approval_timeout_then_poll(rig):
    async def go(call, session, init):
        started = time.monotonic()
        first, _ = await call("ask_approval", {**ASK, "timeout_s": 1})
        waited = time.monotonic() - started
        still, _ = await call("check_approval", {"card_id": first["card_id"]})
        await rig.answer_next("approve")
        after, _ = await call("check_approval", {"card_id": first["card_id"]})
        return first, waited, still, after

    first, waited, still, after = rig.session(go)
    assert first["decision"] == "pending" and 0.9 <= waited < 5 and "check_approval" in first["what_to_do"]
    assert still["decision"] == "pending" and after["decision"] == "approved" and after["card_id"] == first["card_id"]


def test_the_server_cannot_answer_its_own_card_even_with_its_token(rig):
    async def go(call, session, init):
        return await call("ask_approval", {**ASK, "timeout_s": 0})

    result, _ = rig.session(go)
    status, payload = client_for(rig.srv, HOST_TOKEN).call("POST", f"/v1/cards/{result['card_id']}/answer", {"decision": "approve"})
    assert status == 403 and payload["error"]["code"] == "forbidden"
    assert rig.home.cards.get(result["card_id"])["status"] == "pending"


# ---------------------------------------------------------------- input validation and failures


def test_bad_input_is_a_tool_error_and_the_server_carries_on(rig):
    async def go(call, session, init):
        out = [
            await call("ask_approval", {"kind": "save_file", "title": "t"}),
            await call("ask_approval", {**ASK, "timeout_s": 601}),
            await call("ask_approval", {**ASK, "thing": f"key {KEY_CANARY}"}),
            await call("queue_job", {"prompt": f"my key is {KEY_CANARY}"}),
            await call("queue_job", {}),
            await call("job_status", {"id": "../../etc"}),
            await call("check_approval", {"card_id": "0" * 16}),
            await call("no_such_tool", {}),
            await call("resume", {}),
            await call("allowance_status", {"unexpected": True}),
        ]
        out.append(await call("pause_status"))  # still alive
        return out

    out = rig.session(go)
    codes = [result["error"]["code"] if is_error else "ok" for result, is_error in out]
    assert codes == ["invalid_input", "invalid_input", "refused", "refused", "invalid_input", "invalid_input", "not_found",
                     "unknown_tool", "unknown_tool", "invalid_input", "ok"]
    for result, is_error in out[:-1]:
        assert is_error and set(result) == {"error"} and set(result["error"]) <= {"code", "message", "retry_after"}
    assert rig.pending() == [] and rig.home.is_paused() is False
    assert "tool=(unknown) outcome=unknown_tool" in rig.stderr and "no_such_tool" not in rig.stderr


def test_an_unreachable_home_is_a_clear_tool_error_and_no_crash(rig):
    async def go(call, session, init):
        out = [await call(name, args) for name, args in (
            ("allowance_status", {}), ("ask_approval", ASK), ("queue_job", {"prompt": "a"}), ("suggest_service", {}),
            ("record_action", {"action": "a", "summary": "s", "outcome": "done"}))]
        out.append(await call("check_permission", {"action": "read"}))  # needs no home: still answers
        listed = await session.list_tools()
        return out, len(listed.tools)

    out, count = rig.session(go, args=rig.args(socket=rig.tmp / "nobody-home.sock"))
    for result, is_error in out[:-1]:
        assert is_error and result["error"]["code"] == "home_unreachable" and "muse home serve" in result["error"]["message"]
        assert "nobody-home" not in result["error"]["message"]
    assert out[-1][1] is False and out[-1][0]["decision"] == "ask" and count == len(tools.TOOLS)
    assert "outcome=home_unreachable" in rig.stderr


def test_a_home_that_refuses_the_token_or_cannot_answer(start):
    async def go(call, session, init):
        return await call("allowance_status"), await call("ask_approval", {**ASK, "timeout_s": 0})

    rig = start()
    rig.token_file.write_text("a-token-the-home-does-not-know\n", encoding="utf-8")
    for result, is_error in rig.session(go):
        assert is_error and result["error"]["code"] == "unauthorized" and "muse home token add" in result["error"]["message"]
    down = start("error")
    for result, is_error in down.session(go):
        assert is_error and result["error"]["code"] == "home_unavailable" and result["error"]["retry_after"] == 5


# ---------------------------------------------------------------- start-up refusals (no protocol: just the process)


def _run(rig: Rig, args: list[str], env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "muse.mcp", *args], env={**os.environ, **(env or rig.env())}, cwd=str(rig.tmp),
                          stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(os.name != "posix", reason="file modes")
def test_a_world_readable_token_file_stops_the_server_before_it_starts(rig):
    for mode in (0o644, 0o640, 0o604):
        rig.token_file.chmod(mode)
        done = _run(rig, rig.args())
        assert done.returncode == 2 and done.stdout == ""
        assert "not started" in done.stderr and "chmod 600" in done.stderr
        assert HOST_TOKEN not in done.stderr and "CANARY" not in done.stderr and "Traceback" not in done.stderr


def test_no_token_or_a_missing_file_stops_the_server(rig):
    env = {k: v for k, v in os.environ.items() if k != "MUSE_HOME_TOKEN"}
    for args in (["--socket", str(rig.srv.socket_path)], ["--token-file", str(rig.tmp / "missing.token")]):
        done = subprocess.run([sys.executable, "-m", "muse.mcp", *args], env={**env, **rig.env()}, cwd=str(rig.tmp),
                              stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=60)
        assert done.returncode == 2 and done.stdout == "" and "not started" in done.stderr and "Traceback" not in done.stderr


def test_the_token_cannot_be_given_on_the_command_line(rig):
    done = _run(rig, ["--token", HOST_TOKEN])
    assert done.returncode == 2 and done.stdout == ""  # argparse refuses an option that does not exist


def test_stdin_closing_ends_the_server_cleanly(rig):
    done = _run(rig, rig.args())  # stdin is empty: the host went away before saying anything
    assert done.returncode == 0 and done.stdout == "" and "Traceback" not in done.stderr and HOST_TOKEN not in done.stderr


# ---------------------------------------------------------------- the raw protocol, by hand


def _send(proc: subprocess.Popen, message: dict) -> None:
    proc.stdin.write(json.dumps(message) + "\n")
    proc.stdin.flush()


def _read(proc: subprocess.Popen, want_id: int) -> dict:
    while True:
        line = proc.stdout.readline()
        assert line, "the server closed its output"
        message = json.loads(line)  # every line on stdout is one JSON-RPC message, nothing else
        assert message["jsonrpc"] == "2.0"
        if message.get("id") == want_id:
            return message


def test_raw_json_rpc_and_letting_go_of_a_waiting_question_when_the_host_leaves(rig):
    errlog = rig.tmp / "raw-stderr.log"
    with open(errlog, "w", encoding="utf-8") as err:
        proc = subprocess.Popen([sys.executable, "-m", "muse.mcp", *rig.args()], env={**os.environ, **rig.env()}, cwd=str(rig.tmp),
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=err, text=True, encoding="utf-8")
        try:
            _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
                "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "raw-test", "version": "0"}}})
            init = _read(proc, 1)["result"]
            assert init["protocolVersion"] == "2025-06-18" and init["serverInfo"]["name"] == "muse" and "tools" in init["capabilities"]
            _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
            _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
            listed = _read(proc, 2)["result"]["tools"]
            assert [t["name"] for t in listed] == [t.name for t in tools.TOOLS]
            assert all(t["inputSchema"]["type"] == "object" and t["description"] for t in listed)
            _send(proc, {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "check_permission", "arguments": {"action": "send"}}})
            reply = _read(proc, 3)["result"]
            assert reply["isError"] is False and reply["structuredContent"]["decision"] == "ask"
            assert json.loads(reply["content"][0]["text"]) == reply["structuredContent"]
            rig.results.append(json.dumps(reply))
            # A question with a ten-minute wait; then the host goes away. The server must not hang on it.
            _send(proc, {"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "ask_approval", "arguments": {**ASK, "timeout_s": 600}}})
            deadline = time.monotonic() + 15
            while not rig.pending() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert len(rig.pending()) == 1
            proc.stdin.close()
            started = time.monotonic()
            assert proc.wait(timeout=20) == 0
            assert time.monotonic() - started < 15
        finally:
            if proc.poll() is None:
                proc.kill()
            proc.stdout.close()
    rig.stderr += errlog.read_text(encoding="utf-8")
    assert rig.home.cards.get(rig.pending()[0]["id"])["status"] == "pending"  # the question stays for the person
