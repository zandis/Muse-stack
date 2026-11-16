"""Canned behaviour for the app's routes on the fake home (muse/home/fake.py).

The fake home is the real route table over a throw-away folder. Everything in
muse/home/app_api.py that would reach the outside goes through `AppBackend`;
this is the subclass the fake uses. Stores are real (conversations, chores,
settings in the fake's own `fake.env`), so writes behave as on a real home.

The script, by demo state:

  Ask      normal, cards, paused(*)  an answer from "Groq", nothing spent, with a refill time
           privacy `local`           an answer from the AI on this computer (when the state has one)
           privacy `private`         an answer from the demo private service; in `used` and `empty`:
                                     "no connected service keeps this private"
           used                      the three choices: wait until the next refill, hand off, pay US$0.04
                                     (a question of 200 characters or more costs US$1.20, which is over the
                                     default daily limit, so the "raise for today" path can be tried)
           empty                     the router cannot be reached
           error                     every call answers 503, as everywhere on the fake
           (*) paused answers `paused`: the real rule, not canned.
  Connect  the one-tap flow answers `pending` to the first poll and `connected` to the second;
           in paste_code mode any code is accepted.
  Chores   three demo chores (one paused, one spare-allowance); "run now" finishes at once with a
           waiting "Save a file" card that carries a `title_key`.
  Services test answers `ok`; disconnect really removes the service from this fake.

No model is asked, nothing is fetched, no key is stored.
"""

from __future__ import annotations

import json
import secrets
import time
from pathlib import Path
from typing import Any, Mapping

from wardmuse.home import app_ask, app_connect
from wardmuse.home.app_api import AppBackend

PAY_USD = 0.04
BIG_PAY_USD = 1.20
BIG_QUESTION_CHARS = 200
PRIVATE_DEMO_ID = "demo_private"

# A made-up service, so the Private badge can be shown working without saying anything about a real company.
PRIVATE_DEMO_SERVICE: dict[str, Any] = {
    "id": PRIVATE_DEMO_ID, "name": "Private demo service", "connection_method": "paste_key", "minutes_estimate": 2,
    "good_for": ["everyday"], "kind_of_allowance": ["daily"], "checked_on": "2026-11-12", "confidence": "L", "kind": "cloud",
    "tier": "free", "key_env": "DEMO_PRIVATE_API_KEY", "configured": True, "enabled": True,
    "policy": {"trains_on_inputs": "no", "human_review": "no", "region": "unknown", "commercial_use": "unknown", "terms_url": None,
               "note": "Demo data on a fake home: this service does not exist."},
    "state": "ok", "blocked_until": None, "block_reason": None,
    "usage": {"minute": {"requests": 0, "tokens": 0}, "day": {"requests": 80, "tokens": 51000}, "month": {"requests": 420, "tokens": 300000}},
    "limits": [{"window": "day", "metric": "requests", "limit": 500, "remaining": 420, "reset_at": "2026-11-12T16:00:00Z", "source": "estimate"}],
    "deployments": [],
}


CUT_PREFIXES = ("write a long", "寫一篇長")
_LONG_EN = ("(fake home) A long answer, in Markdown.\n\n## Plan\n\n1. **Monday**: read the lease.\n2. *Tuesday*: call the landlord.\n"
            "3. Wednesday: pay `NT$18,000`.\n\n| Day | Task |\n| --- | --- |\n| Mon | Read |\n| Tue | Call |\n\n"
            "```\nrent = 18000\n```\n\nThat is the whole plan, from start to end.")
_LONG_ZH = ("（假的家）一篇比較長的答案。\n\n## 計畫\n\n1. **星期一**：讀租約。\n2. *星期二*：打電話給房東。\n"
            "3. 星期三：付 `NT$18,000`。\n\n| 日 | 事 |\n| --- | --- |\n| 一 | 讀 |\n| 二 | 打 |\n\n```\nrent = 18000\n```\n\n這就是完整的計畫。")


def _zh(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)


def _completion(text: str, tokens_in: int, tokens_out: int) -> bytes:
    return json.dumps({"choices": [{"message": {"role": "assistant", "content": text}}],
                       "usage": {"prompt_tokens": tokens_in, "completion_tokens": tokens_out}}, ensure_ascii=False).encode("utf-8")


def _refusal(message: str) -> bytes:
    return json.dumps({"error": {"message": message}}).encode("utf-8")


class FakeFlows:
    """The one-tap flow without a browser or a listener: `pending` on the first poll, `connected` on the second."""

    def __init__(self, home: Any) -> None:
        self.home = home
        self._flows: dict[str, app_connect.Flow] = {}
        self._polls: dict[str, int] = {}

    def start(self, provider: Any, mode: str) -> app_connect.Flow:
        conn = provider.connection
        if conn is None or conn.method != "oauth_pkce":
            raise app_connect.FlowError("invalid_request", "That service is not connected in one tap. Use the guided steps and paste its key.")
        now = self.home.clock()
        flow_id = secrets.token_hex(8)
        # The real address shape, pointing nowhere a fake could be approved: the app's tests never open it.
        url = f"{conn.auth_url}?code_challenge=fake-home&code_challenge_method=S256&key_label=Muse"
        flow = app_connect.Flow(flow_id, provider.id, mode, url, now, now + app_connect.TIMEOUT_S)
        self._flows[flow_id], self._polls[flow_id] = flow, 0
        return flow

    def _connect(self, flow: app_connect.Flow) -> None:
        provider = next(p for p in self.home.load_catalogue().providers if p.id == flow.provider)
        self.home.save_key(str(provider.env), "")
        flow.status, flow.finished_at = "connected", self.home.clock()

    def get(self, flow_id: str) -> app_connect.Flow | None:
        flow = self._flows.get(flow_id)
        if flow is None:
            return None
        if flow.status == "pending" and flow.mode == "loopback":
            self._polls[flow_id] += 1
            if self._polls[flow_id] >= 2:
                self._connect(flow)
        return flow

    def give_code(self, flow_id: str, provider_of: Any, code: str) -> app_connect.Flow:
        flow = self._flows.get(flow_id)
        if flow is None:
            raise app_connect.FlowError("not_found", "There is no such connection in progress.")
        if flow.mode != "paste_code" or flow.status != "pending":
            raise app_connect.FlowError("conflict", "This connection takes no code now. Start again.")
        if not code:
            raise app_connect.FlowError("invalid_request", "That does not look like the code the service showed. Copy it again.")
        self._connect(flow)
        return flow

    def cancel(self, flow_id: str) -> bool:
        flow = self._flows.get(flow_id)
        if flow is not None and flow.status == "pending":
            flow.status, flow.reason, flow.finished_at = "failed", "cancelled", self.home.clock()
        return flow is not None

    def close(self) -> None:
        self._flows.clear()


class FakeApp(AppBackend):
    def __init__(self, home: Any) -> None:
        super().__init__(home)
        self.flows = FakeFlows(home)  # type: ignore[assignment]

    def write_setting(self, name: str, value: str) -> None:
        """Into the fake's own throw-away settings file, never the real key store."""
        from wardmuse import keys

        keys._dotenv_set(name, value, self.home.environ)  # noqa: SLF001

    # ---- the router, scripted

    def router_send(self, messages: list[dict[str, str]], *, privacy: str | None, task: str | None, allow_paid: bool,
                    max_tokens: int = app_ask.DEFAULT_MAX_TOKENS) -> app_ask.RouterReply:
        state = self.home.state
        question = messages[-1]["content"]
        zh = _zh(question)
        now = self.home.clock()
        connected = {p["id"]: p for p in self.home.snapshot()["providers"] if p.get("configured")}
        if state == "empty":
            if not connected:
                return app_ask.RouterReply(0)  # nothing is connected and no router runs
            # First run: the service that was just connected answers.
            first = next(iter(connected))
            text = "（假的家）這是你剛連接的服務回答的示範答案。" if zh else "(fake home) This is a canned answer from the service you just connected."
            return app_ask.RouterReply(200, {"x-muse-provider": first, "x-muse-deployment": f"{first}-demo", "x-muse-cost-usd": "0",
                                             "x-muse-privacy": privacy or "standard", "x-muse-task": task or "everyday"},
                                       _completion(text, 30, 58))
        if question.strip().lower().startswith(CUT_PREFIXES) and state in ("normal", "cards", "paused"):
            # A long answer that stops at the length limit, then comes whole when asked again with more room.
            cut = max_tokens <= app_ask.DEFAULT_MAX_TOKENS
            text = _LONG_ZH if zh else _LONG_EN
            body = json.dumps({"choices": [{"message": {"role": "assistant", "content": text[: len(text) // 2] if cut else text},
                                            "finish_reason": "length" if cut else "stop"}],
                               "usage": {"prompt_tokens": 40, "completion_tokens": max_tokens if cut else 900}}, ensure_ascii=False).encode()
            return app_ask.RouterReply(200, {"x-muse-provider": "groq", "x-muse-deployment": "groq-gpt-oss-20b", "x-muse-cost-usd": "0",
                                             "x-muse-privacy": privacy or "standard", "x-muse-task": task or "everyday"}, body)
        if privacy == "local":
            if "ollama" not in connected:
                return app_ask.RouterReply(403, {"x-muse-privacy": "local"}, _refusal("[muse] no deployment may receive a local request"))
            text = "（假的家）這是由這台電腦上的 AI 回答的示範答案。" if zh else "(fake home) This is a canned answer from the AI on this computer."
            return app_ask.RouterReply(200, {"x-muse-provider": "ollama", "x-muse-deployment": "ollama-qwen3", "x-muse-cost-usd": "0",
                                             "x-muse-privacy": "local", "x-muse-task": task or "everyday"}, _completion(text, 38, 61))
        if privacy == "private":
            if state == "used" or PRIVATE_DEMO_ID not in connected:
                return app_ask.RouterReply(403, {"x-muse-privacy": "private"}, _refusal("[muse] no deployment may receive a private request"))
            text = "（假的家）這是只送到「不拿去訓練、也沒有人會看」的服務的示範答案。" if zh else \
                "(fake home) This is a canned answer that went only to a service that does not train on it or let people read it."
            return app_ask.RouterReply(200, {"x-muse-provider": PRIVATE_DEMO_ID, "x-muse-deployment": "demo-private-chat",
                                             "x-muse-cost-usd": "0", "x-muse-privacy": "private", "x-muse-task": task or "everyday"},
                                       _completion(text, 44, 70))
        if state == "used":
            pay = BIG_PAY_USD if len(question) >= BIG_QUESTION_CHARS else PAY_USD
            if allow_paid:
                text = "（假的家）這是付費服務回答的示範答案。" if zh else "(fake home) This is a canned answer from a service you agreed to pay for."
                return app_ask.RouterReply(200, {"x-muse-provider": "deepseek", "x-muse-deployment": "deepseek-chat",
                                                 "x-muse-cost-usd": f"{pay:.2f}", "x-muse-privacy": privacy or "standard",
                                                 "x-muse-task": task or "everyday"}, _completion(text, 52, 180))
            until = app_ask.iso(now + 2 * 3600 + 600)
            message = (f"[muse] free quota is used up until {until}; a paid service could answer"
                       f" | options=wait:{until},handoff,pay:{pay:.2f}")
            return app_ask.RouterReply(429, {"x-muse-ration": "defer:quota", "retry-after": "7800", "x-muse-privacy": privacy or "standard"},
                                       _refusal(message))
        text = ("（假的家）這是示範答案。真正的家會把你的問題交給已連接的服務，這裡沒有詢問任何模型。" if zh else
                "(fake home) This is a canned answer. A real home would send your question to a connected service; "
                "no model was asked here.")
        return app_ask.RouterReply(200, {"x-muse-provider": "groq", "x-muse-deployment": "groq-gpt-oss-20b", "x-muse-cost-usd": "0",
                                         "x-muse-privacy": privacy or "standard", "x-muse-task": task or "everyday"},
                                   _completion(text, 41, 96))

    # ---- chores

    def chore_env(self) -> Mapping[str, str]:
        return dict(self.home.environ or {})

    def run_chore(self, store: Any, run: Any) -> None:
        from wardmuse.skills import chores

        chore = store.get(run.chore_id)
        lang = chore.spec.lang
        name = f"Muse {time.strftime('%Y-%m-%d', time.gmtime(self.home.clock()))}.md"
        where = chore.spec.dest or str(Path(str(self.home.demo_root)) / "documents")
        label = "每週摘要" if lang == "zh-TW" else "Weekly digest"
        card_id = self.home.cards.create(
            kind="save_file", title=chores.w("card_title", lang), thing=name, where=where,
            under=chores.w("card_under", lang, label=label), extra={"size_bytes": 2048}, job_ref=f"chore:{chore.id}:{run.id}",
            title_key="save_file_chore", params={"label": label})
        counts = chores.w("counts", lang, done=3, missed=0)
        chores._finish(store, run, "waiting_card", f"{counts}; {chores.w('waiting', lang)}", card_id=card_id,  # noqa: SLF001
                       out_path=str(Path(where) / name), requests=3, tokens=5200, cost_usd=0.0)

    # ---- services

    def test_key(self, provider: Any) -> dict[str, Any]:
        return {"result": "ok", "status": 200}

    def remove_key(self, provider: Any) -> list[str]:
        self.home.forget_service(provider)
        return ["fake"]

    # ---- the proxy, verify, versions

    def manages_proxy(self) -> tuple[bool, list[str]]:
        return True, []

    def restart_proxy(self) -> dict[str, Any]:
        self._restart_pending = False
        return {"restarted": True, "command": self.home.restart_command(True)}

    def verify(self) -> dict[str, Any]:
        return {"result": "ok", "lines": [
            {"level": "ok", "text": "(fake home) the installed router is the pinned version and its files match their record"},
            {"level": "ok", "text": "(fake home) no known sign of the March 2026 malware was found"},
        ]}

    def versions(self) -> dict[str, Any]:
        out = super().versions()
        out["router"] = "(fake home)"
        return out
