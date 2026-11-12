"""`muse connect`: guided ways to add a helper, and `muse resources`.

Why this module exists: the first connection must need no key pasting, and every
later one must be a short, honest walk-through. Muse GUIDES. It never fills in,
clicks or signs in on a provider's website, and it never reads a key from the
command line (argv shows up in shell history and `ps`).

What is here:
- `connect_oauth`: OpenRouter's one-tap OAuth PKCE flow. A loopback listener
  on 127.0.0.1 receives the redirect; `--paste-code` is the headless fallback.
- `connect_paste_key`: steps from the catalogue, then a hidden prompt for the key.
- `connect_local`: wraps local-model discovery.
- `show_overview`: what is connected and the ranked next suggestions.
- `run_resources`: the read-only view of non-AI allowances and subscriptions.

Secrets: a code or key lives in local variables only. Nothing here prints,
logs, formats into an exception, or stores a code or key anywhere but
`muse.keys`. Response bodies from the provider are never shown.

Standard library only (plus the modules it imports).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import http.server
import json
import re
import secrets
import socketserver
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from muse import __version__, catalog, keys, local, paths
from muse.catalog import Provider, tr

MAX_BODY = 65536  # most bytes read from a provider's reply
DEFAULT_WAIT_S = 300.0  # OpenRouter's code lives 10 minutes; waiting 5 is plenty
MAX_REJECTED = 200  # stray requests tolerated before the listener gives up
CALLBACK_PREFIX = "/cb/"
LOOPBACK_HOST = "127.0.0.1"
OPENROUTER = "openrouter"


class ConnectError(Exception):
    """A user-fixable problem. The message never contains a code, key or provider reply."""


class Cancelled(ConnectError):
    """The user stopped, or declined on the provider's page. Nothing was saved."""


# ------------------------------------------------------------------ strings

CONNECT_STRINGS: dict[str, dict[str, str]] = {
    "en": {
        "overview_connected": "Connected helpers: {names}",
        "overview_none": "No helper is connected yet.",
        "overview_local": "AI on this computer: {n} model(s) known",
        "overview_header": "Next helpers to add, best first:",
        "overview_none_left": "You have connected every helper Muse can suggest.",
        "overview_item": "{rank}. {name}: {meta}",
        "overview_gain": "   {gain}{friction}",
        "overview_steps": "   Steps: {steps}",
        "overview_run": "   Run: muse connect {id}",
        "overview_note": "Muse only guides you. It never fills in or clicks anything on a provider's website.",
        "unknown_helper": "Muse does not know that helper. Run `muse connect` to see the list.",
        "not_a_name": "That does not look like a helper name. Muse never takes a key on the command line; it asks for it with hidden input. Run `muse connect` to see the list.",
        "guide_title": "Connecting {name}",
        "guide_only": "Muse only guides you. It never fills in or clicks anything on {name}'s website; you do each step yourself.",
        "guide_gain": "What you get: {gain}.",
        "guide_friction": "Heads-up: {friction}.",
        "guide_steps": "Steps:",
        "ask_open": "Open the {name} key page in your browser now? [y/N] ",
        "opening": "Opening {url}",
        "open_yourself": "Open this page yourself: {url}",
        "prompt_key": "Paste your {name} key (input is hidden): ",
        "prompt_extra": "Paste {label} (not a secret; shown as you type): ",
        "no_terminal": "There is no terminal to type the key into. Run `muse connect {id}` in a terminal instead.",
        "empty_value": "Nothing was pasted. Nothing was saved.",
        "bad_value": "That value cannot be saved ({problem}). Nothing was saved.",
        "shape_ask": "This does not look like a {name} key (those start with {prefix}). Save it anyway? [y/N] ",
        "saved": "Saved to {location}. The key was not shown anywhere.",
        "saved_extra": "Saved {label} to {location}.",
        "declined": "Not saved.",
        "test_intro": "Test (you asked for --test): Muse will send ONE request: GET {url}, with your key in the {auth}. It sends no prompt text and uses no model allowance.",
        "test_auth_bearer": "Authorization header",
        "test_auth_header": "{name} header",
        "test_ok": "{name} accepted the key.",
        "test_rejected": "{name} answered {status}: the key was not accepted. Check that you copied all of it.",
        "test_limited": "{name} answered 429 (rate limited), so the key probably works but the allowance is used up for now.",
        "test_other": "{name} answered {status}. That does not say whether the key works.",
        "test_failed": "The test could not reach {name} ({kind}). The key is saved; nothing else was sent.",
        "test_none": "No safe test is defined for {name} yet, so nothing was sent. Use `muse ask` once the proxy has restarted to try it.",
        "test_hint": "Tip: add --test to send one key-check request.",
        "gained": "Now connected. {gain}.",
        "restart_running": "Restart the proxy so it picks up the new key: {cmd}",
        "restart_stopped": "The proxy is not running. Start it with: {cmd}",
        "next_best": "Next best helper: {name} ({meta}). Run: muse connect {id}",
        "oa_title": "Connecting {name} with one tap",
        "oa_intro": "Muse opens {name}'s own page in your browser. You approve there, and {name} sends the key straight to this computer. You do not copy or paste anything. Muse does not click anything for you.",
        "oa_url": "If the browser did not open, open this link yourself:\n{url}",
        "oa_wait": "Waiting for your approval (up to {minutes} min). Press Ctrl-C to cancel.",
        "oa_timeout": "Nobody approved in time, so Muse stopped waiting. Nothing was saved. Run the command again when you are ready.",
        "oa_cancelled": "Cancelled. Nothing was saved.",
        "oa_denied": "You did not approve on {name}'s page. Nothing was saved.",
        "oa_bad_callback": "The browser sent something Muse did not expect, so it stopped listening. Nothing was saved. Try again.",
        "oa_too_many": "Too many unexpected requests reached the local listener, so Muse stopped. Nothing was saved.",
        "oa_exchange_failed": "{name} would not hand over a key ({reason}). Nothing was saved. Try again, or paste a key yourself with: muse keys set {env}",
        "oa_bind_failed": "Muse could not open a local listener ({kind}). Use --paste-code instead.",
        "oa_paste_intro": "Open this link in any browser (it can be on another device), approve, and {name} will show a code on screen:\n{url}",
        "oa_paste_prompt": "Paste the code (input is hidden): ",
        "oa_paste_empty": "Nothing was pasted. Nothing was saved.",
        "oa_probe": "Free requests left today: {remaining} of {limit}.",
        "oa_probe_unknown": "Could not check how much is left right now. That is fine; try `muse status --probe` later.",
        "oa_caveat_small": "Honest note 1: the free allowance is small: {small} requests a day until you have bought credits on OpenRouter ({big} a day after that).",
        "oa_caveat_small_plain": "Honest note 1: the free allowance is small until you have bought credits on OpenRouter.",
        "oa_caveat_learn": "Honest note 2: some free models keep or learn from what you send, so do not send anything private through them.",
        "reason_http": "HTTP {status}",
        "reason_tls": "its certificate could not be verified",
        "reason_redirect": "it tried to redirect",
        "reason_size": "the reply was too large",
        "reason_shape": "the reply was not what Muse expected",
        "reason_network": "network error: {kind}",
        "local_title": "Connecting AI on this computer",
        "local_found": "Found {n} model(s) on this computer.",
        "local_none": "No local runtime answered. Start Ollama or LM Studio (or set MUSE_LOCAL_OPENAI_BASE), then run this again.",
        "local_wrote": "Wrote {path}. Set the variables below in your .env, then restart the proxy.",
        "local_dry": "Nothing was written (--no-write).",
        "res_title": "Other allowances (shown, not managed yet)",
        "res_sub": "Shown only. Muse cannot manage these for you yet.",
        "res_tag": "Shown, not managed yet",
        "res_how": "How it would be connected: {how}",
        "res_checked": "Checked {date}, confidence {conf} ({conf_text})",
        "res_kind_storage": "Storage",
        "res_kind_compute": "Compute and hosting",
        "res_kind_chain": "Blockchain and transactions",
        "res_none": "No other allowances are in the catalogue yet.",
        "sub_title": "Your subscriptions (hand-off only)",
        "sub_sub": "Muse never signs in to these. It only opens them with your question.",
        "sub_tag": "Hand-off only",
        "sub_open_web": "Opens: {url}",
        "sub_open_app": "Desktop app link: {url}",
        "sub_prefill_yes": "The app link can carry your question (documented by the provider).",
        "sub_prefill_no": "Opens the plain page; a prefilled question is not confirmed.",
    },
    "zh-TW": {
        "overview_connected": "已連接的幫手：{names}",
        "overview_none": "目前還沒有連接任何幫手。",
        "overview_local": "這台電腦上的 AI：已知 {n} 個模型",
        "overview_header": "建議接下來新增的幫手（最划算的在前）：",
        "overview_none_left": "Muse 能建議的幫手你都已經連接了。",
        "overview_item": "{rank}. {name}：{meta}",
        "overview_gain": "   {gain}{friction}",
        "overview_steps": "   步驟：{steps}",
        "overview_run": "   執行：muse connect {id}",
        "overview_note": "Muse 只會引導你，不會在服務商的網站上替你填寫或點擊任何東西。",
        "unknown_helper": "Muse 不認識這個幫手。執行 `muse connect` 可以看到清單。",
        "not_a_name": "這看起來不像幫手的名稱。Muse 不會從命令列取得金鑰，而是用隱藏輸入向你詢問。執行 `muse connect` 可以看到清單。",
        "guide_title": "連接 {name}",
        "guide_only": "Muse 只會引導你，不會在 {name} 的網站上替你填寫或點擊任何東西；每一步都由你自己完成。",
        "guide_gain": "連接後可得到：{gain}。",
        "guide_friction": "先說一聲：{friction}。",
        "guide_steps": "步驟：",
        "ask_open": "現在要在瀏覽器打開 {name} 的金鑰頁面嗎？[y/N] ",
        "opening": "正在打開 {url}",
        "open_yourself": "請自己打開這個頁面：{url}",
        "prompt_key": "貼上你的 {name} 金鑰（輸入時不會顯示）：",
        "prompt_extra": "貼上 {label}（不是機密，輸入時會顯示）：",
        "no_terminal": "這裡沒有可以輸入金鑰的終端機。請改在終端機執行 `muse connect {id}`。",
        "empty_value": "沒有貼上任何內容，也沒有儲存任何東西。",
        "bad_value": "這個值無法儲存（{problem}）。沒有儲存任何東西。",
        "shape_ask": "這看起來不像 {name} 的金鑰（應該以 {prefix} 開頭）。仍要儲存嗎？[y/N] ",
        "saved": "已儲存到 {location}。金鑰沒有顯示在任何地方。",
        "saved_extra": "已把 {label} 儲存到 {location}。",
        "declined": "沒有儲存。",
        "test_intro": "測試（你加了 --test）：Muse 會送出一個請求：GET {url}，並在 {auth} 帶上你的金鑰。不會送出任何提示文字，也不會用掉模型額度。",
        "test_auth_bearer": "Authorization 標頭",
        "test_auth_header": "{name} 標頭",
        "test_ok": "{name} 接受了這把金鑰。",
        "test_rejected": "{name} 回應 {status}：金鑰沒有被接受。請確認有複製完整。",
        "test_limited": "{name} 回應 429（次數受限），金鑰多半可用，但目前額度已用完。",
        "test_other": "{name} 回應 {status}，這無法說明金鑰是否可用。",
        "test_failed": "測試連不到 {name}（{kind}）。金鑰已儲存，沒有再送出其他東西。",
        "test_none": "目前還沒有為 {name} 定義安全的測試，所以什麼都沒送出。代理重新啟動後，可以用 `muse ask` 試試看。",
        "test_hint": "小提示：加上 --test 可以送出一個確認金鑰的請求。",
        "gained": "已連接。{gain}。",
        "restart_running": "請重新啟動代理，讓它讀到新的金鑰：{cmd}",
        "restart_stopped": "代理目前沒有在執行。用這個指令啟動：{cmd}",
        "next_best": "下一個最划算的幫手：{name}（{meta}）。執行：muse connect {id}",
        "oa_title": "一鍵連接 {name}",
        "oa_intro": "Muse 會在你的瀏覽器打開 {name} 自己的頁面。你在那裡核准，{name} 就會把金鑰直接交給這台電腦。你不用複製或貼上任何東西。Muse 不會替你點擊任何東西。",
        "oa_url": "如果瀏覽器沒有打開，請自己打開這個連結：\n{url}",
        "oa_wait": "正在等你核准（最久 {minutes} 分鐘）。按 Ctrl-C 可以取消。",
        "oa_timeout": "時間內沒有人核准，所以 Muse 不再等待。沒有儲存任何東西。準備好時再執行一次指令。",
        "oa_cancelled": "已取消。沒有儲存任何東西。",
        "oa_denied": "你沒有在 {name} 的頁面核准。沒有儲存任何東西。",
        "oa_bad_callback": "瀏覽器送來了 Muse 沒預期的內容，所以它停止了監聽。沒有儲存任何東西。請再試一次。",
        "oa_too_many": "本機監聽收到太多不預期的請求，所以 Muse 停止了。沒有儲存任何東西。",
        "oa_exchange_failed": "{name} 沒有交出金鑰（{reason}）。沒有儲存任何東西。請再試一次，或自己貼上金鑰：muse keys set {env}",
        "oa_bind_failed": "Muse 無法開啟本機監聽（{kind}）。請改用 --paste-code。",
        "oa_paste_intro": "請在任何瀏覽器打開這個連結（可以在另一台裝置上），核准後 {name} 會在畫面上顯示一組代碼：\n{url}",
        "oa_paste_prompt": "貼上代碼（輸入時不會顯示）：",
        "oa_paste_empty": "沒有貼上任何內容。沒有儲存任何東西。",
        "oa_probe": "今天還剩 {remaining}／{limit} 次免費請求。",
        "oa_probe_unknown": "現在查不到還剩多少。沒關係，稍後可以執行 `muse status --probe`。",
        "oa_caveat_small": "老實說之一：免費額度很小：在你於 OpenRouter 買過額度之前，每天只有 {small} 次請求（買過之後是每天 {big} 次）。",
        "oa_caveat_small_plain": "老實說之一：在你於 OpenRouter 買過額度之前，免費額度很小。",
        "oa_caveat_learn": "老實說之二：有些免費模型會保留或學習你送出的內容，所以不要透過它們送任何私密資料。",
        "reason_http": "HTTP {status}",
        "reason_tls": "無法驗證它的憑證",
        "reason_redirect": "它嘗試轉址",
        "reason_size": "回應太大",
        "reason_shape": "回應不是 Muse 預期的樣子",
        "reason_network": "網路錯誤：{kind}",
        "local_title": "連接這台電腦上的 AI",
        "local_found": "在這台電腦上找到 {n} 個模型。",
        "local_none": "沒有任何本機執行環境回應。請先啟動 Ollama 或 LM Studio（或設定 MUSE_LOCAL_OPENAI_BASE），再執行一次。",
        "local_wrote": "已寫入 {path}。請把下面的變數設到你的 .env，然後重新啟動代理。",
        "local_dry": "沒有寫入任何東西（--no-write）。",
        "res_title": "其他額度（僅顯示，尚未管理）",
        "res_sub": "只顯示，Muse 還不能代你管理。",
        "res_tag": "僅顯示，尚未管理",
        "res_how": "連接方式：{how}",
        "res_checked": "核對日期 {date}，把握程度 {conf}（{conf_text}）",
        "res_kind_storage": "儲存空間",
        "res_kind_compute": "運算與主機",
        "res_kind_chain": "區塊鏈與交易",
        "res_none": "目錄裡還沒有其他額度。",
        "sub_title": "你的訂閱（只能交接）",
        "sub_sub": "Muse 不會登入這些帳號，只會帶著你的問題幫你打開它們。",
        "sub_tag": "只能交接",
        "sub_open_web": "打開：{url}",
        "sub_open_app": "桌面 App 連結：{url}",
        "sub_prefill_yes": "App 連結可以帶著你的問題（服務商有公開說明）。",
        "sub_prefill_no": "只會打開一般頁面；能否預填問題尚未確認。",
    },
}


def ct(key: str, lang: str, **fmt: Any) -> str:
    """A connect-flow string in `lang` (English fallback). Never raises."""
    table = CONNECT_STRINGS.get(lang) or CONNECT_STRINGS[catalog.DEFAULT_LANG]
    text = table.get(key) or CONNECT_STRINGS[catalog.DEFAULT_LANG].get(key) or key
    try:
        return text.format(**fmt)
    except (KeyError, IndexError, ValueError):
        return text


# ----------------------------------------------------------------------- io


@dataclass
class ConnectIO:
    """Everything that touches the person or the network, so tests can fake it."""

    out: TextIO = field(default_factory=lambda: sys.stdout)
    err: TextIO = field(default_factory=lambda: sys.stderr)
    secret: Callable[[str], str] | None = None  # hidden prompt; None means no terminal
    text: Callable[[str], str] | None = None  # visible prompt
    confirm: Callable[[str, bool], bool] | None = None
    open_url: Callable[[str], bool] = lambda url: webbrowser.open(url)
    http_get: Callable[[str, dict[str, str], float], int] | None = None
    probe: Callable[[str], list[dict[str, Any]]] | None = None  # OpenRouter remaining-quota probe
    proxy_running: Callable[[], bool] | None = None
    monotonic: Callable[[], float] = time.monotonic
    tick: Callable[[], None] | None = None  # called while waiting; tests raise KeyboardInterrupt here

    def say(self, text: str = "") -> None:
        print(text, file=self.out)

    def warn(self, text: str) -> None:
        print(text, file=self.err)

    @classmethod
    def system(cls) -> "ConnectIO":
        """Real terminal I/O. Hidden input only when stdin is a terminal."""
        import getpass

        tty = bool(getattr(sys.stdin, "isatty", lambda: False)())

        def secret(prompt: str) -> str:
            return getpass.getpass(prompt)

        def text(prompt: str) -> str:
            return input(prompt)

        def confirm(prompt: str, default: bool) -> bool:
            answer = input(prompt).strip().lower()
            return default if not answer else answer in ("y", "yes", "是", "好")

        return cls(secret=secret if tty else None, text=text if tty else None, confirm=confirm if tty else None)


# ----------------------------------------------------------------- catalogue


def _load() -> catalog.Catalog:
    return catalog.load_catalog()


def connected_ids(env: Mapping[str, str], providers: tuple[Provider, ...]) -> set[str]:
    """Providers whose key (and extra settings) are present in the key store or environment."""
    out = set()
    for p in providers:
        if p.local or not p.env:
            continue
        if all(keys.is_configured(n, env) for n in (p.env, *p.also_env)):
            out.add(p.id)
    return out


def find_provider(providers: tuple[Provider, ...], name: str) -> Provider | None:
    return next((p for p in providers if p.id == name and not p.local), None)


_NAME_OK = re.compile(r"^[a-z][a-z0-9_]{0,23}$")


def looks_like_name(text: str) -> bool:
    """A helper id is short lower-case words. Anything else might be a pasted key, so it is never echoed."""
    return bool(_NAME_OK.match(text or ""))


# ------------------------------------------------------------------ overview


def show_overview(io: ConnectIO, env: Mapping[str, str], lang: str, *, as_json: bool = False) -> int:
    """What is connected, then the ranked suggestions with time, steps and gain."""
    cat = _load()
    connected = connected_ids(env, cat.providers)
    suggestions = catalog.suggest_next(connected, env, providers=cat.providers)
    if as_json:
        names = [p.name for p in cat.providers if p.id in connected]
        io.say(json.dumps({"connected": sorted(connected), "suggestions": [s.to_dict() for s in suggestions]}, indent=2, ensure_ascii=False))
        return 0
    names = [p.name for p in cat.providers if p.id in connected]
    io.say(ct("overview_connected", lang, names=", ".join(names)) if names else ct("overview_none", lang))
    io.say()
    if not suggestions:
        io.say(ct("overview_none_left", lang))
    else:
        io.say(ct("overview_header", lang))
        by_id = {p.id: p for p in cat.providers}
        for s in suggestions:
            io.say(ct("overview_item", lang, rank=s.rank, name=s.name, meta=s.meta_text(lang)))
            # Only a definite card or phone need is worth a line here; "not confirmed" is shown when you connect.
            friction = s.friction_text(lang) if "yes" in (s.card, s.phone) else None
            io.say(ct("overview_gain", lang, gain=s.gain_text(lang), friction=f" · {friction}" if friction else ""))
            prov = by_id.get(s.provider_id)
            if prov and prov.connection and prov.connection.steps:
                titles = " > ".join(catalog.pick(step.title, lang) for step in prov.connection.steps)
                io.say(ct("overview_steps", lang, steps=titles))
            io.say(ct("overview_run", lang, id=s.provider_id))
    io.say()
    io.say(ct("overview_note", lang))
    return 0


# --------------------------------------------------------------- after-care


def proxy_command(env: Mapping[str, str], running: bool) -> str:
    """The exact command that makes the proxy read the new key (see muse/cli.py cmd_restart).

    A pid file means a detached native proxy; otherwise Docker is the default.
    `muse restart` recreates the container with the current keys.
    """
    native = (paths.home(env) / "proxy.pid").exists()
    if running:
        return "muse restart --native --detach" if native else "muse restart"
    return "muse up --native --detach" if native else "muse up"


def _proxy_running(io: ConnectIO, env: Mapping[str, str]) -> bool:
    if io.proxy_running is not None:
        return bool(io.proxy_running())
    try:
        from muse import status

        url = status._proxy_url(env)  # noqa: SLF001 - same answer the status screen gives
        return bool(status._reachable(url))  # noqa: SLF001
    except Exception:
        return False


def after_connect(io: ConnectIO, env: Mapping[str, str], lang: str, just_connected: str | None) -> None:
    """Say how the proxy picks the key up, and what to add next."""
    running = _proxy_running(io, env)
    cmd = proxy_command(env, running)
    io.say(ct("restart_running" if running else "restart_stopped", lang, cmd=cmd))
    cat = _load()
    fresh_env = dict(env)
    connected = connected_ids(fresh_env, cat.providers)
    if just_connected:
        connected.add(just_connected)
    nxt = catalog.suggest_next(connected, env, providers=cat.providers)
    io.say()
    if nxt:
        first = nxt[0]
        io.say(ct("next_best", lang, name=first.name, meta=first.meta_text(lang), id=first.provider_id))
    else:
        io.say(ct("overview_none_left", lang))


def _gain_for(provider: Provider, lang: str) -> str:
    return catalog.gain_text(provider.headline(), lang)


# ------------------------------------------------------------- PKCE helpers


def pkce_pair() -> tuple[str, str]:
    """(verifier, S256 challenge). RFC 7636: 43 to 128 unreserved characters; this is 64."""
    verifier = secrets.token_urlsafe(48)
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return verifier, base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


@dataclass(frozen=True)
class Endpoints:
    """Where the OAuth flow talks. Production values come from the catalogue and must be https."""

    auth_url: str
    exchange_url: str
    allow_insecure: bool = False  # tests only; never set by the CLI

    def check(self) -> None:
        for url in (self.auth_url, self.exchange_url):
            parts = urllib.parse.urlsplit(url)
            if parts.scheme == "https" and parts.netloc:
                continue
            if self.allow_insecure and parts.scheme in ("http", "https") and parts.netloc:
                continue
            raise ConnectError("The provider's connection address is not https, so Muse will not use it.")


def build_auth_url(auth_url: str, challenge: str, *, callback_url: str | None, state: str | None, label: str = "Muse") -> str:
    """The page the person approves on. No callback_url means the provider shows a code instead (headless)."""
    query: dict[str, str] = {"code_challenge": challenge, "code_challenge_method": "S256", "key_label": label}
    if callback_url:
        query["callback_url"] = callback_url
    if state:
        query["state"] = state
    sep = "&" if "?" in auth_url else "?"
    return auth_url + sep + urllib.parse.urlencode(query)


# ---------------------------------------------------------- loopback receiver

_PAGE = (
    b"<!doctype html><meta charset=utf-8><title>Muse</title>"
    b"<p>Muse received the answer. You can close this tab and go back to the terminal.</p>"
    b"<p>Muse \xe5\xb7\xb2\xe6\x94\xb6\xe5\x88\xb0\xe5\x9b\x9e\xe6\x87\x89\xef\xbc\x8c"
    b"\xe5\x8f\xaf\xe4\xbb\xa5\xe9\x97\x9c\xe6\x8e\x89\xe9\x80\x99\xe5\x80\x8b\xe5\x88\x86\xe9\xa0\x81\xe3\x80\x82</p>"
)
_NOT_FOUND = b"Not found"
_CODE_RE = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")


class _Handler(http.server.BaseHTTPRequestHandler):
    server_version = "Muse"
    sys_version = ""
    protocol_version = "HTTP/1.0"
    timeout = 5  # a client that stalls cannot hold the listener

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass  # the path carries a secret and the query carries the code: never log either

    def _reply(self, status: int, body: bytes, ctype: str = "text/plain; charset=utf-8") -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'none'")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:  # noqa: N802
        srv: _Receiver = self.server  # type: ignore[assignment]
        srv.seen += 1
        host = (self.headers.get("Host") or "").strip().lower()
        if host not in srv.allowed_hosts:
            self._reply(403, _NOT_FOUND)
            return
        parts = urllib.parse.urlsplit(self.path)
        expected = CALLBACK_PREFIX + srv.path_token
        if parts.netloc or parts.scheme or not hmac.compare_digest(parts.path.encode("utf-8", "replace"), expected.encode("ascii")):
            self._reply(404, _NOT_FOUND)
            return
        try:
            pairs = urllib.parse.parse_qsl(parts.query, keep_blank_values=True, strict_parsing=bool(parts.query), max_num_fields=10)
        except ValueError:
            srv.outcome = ("bad", None)
            self._reply(400, _NOT_FOUND)
            return
        params: dict[str, str] = {}
        for k, v in pairs:
            if k in params:  # a repeated name is how parameters get smuggled
                srv.outcome = ("bad", None)
                self._reply(400, _NOT_FOUND)
                return
            params[k] = v
        if "code" not in params:
            if "error" in params:  # the person declined on the provider's page
                srv.outcome = ("denied", None)
                self._reply(200, _PAGE, "text/html; charset=utf-8")
            else:
                srv.outcome = ("bad", None)
                self._reply(400, _NOT_FOUND)
            return
        given_state = params.get("state", "")
        if not hmac.compare_digest(given_state.encode("utf-8", "replace"), srv.state.encode("ascii")):
            srv.outcome = ("bad", None)
            self._reply(400, _NOT_FOUND)
            return
        code = params["code"]
        if not _CODE_RE.match(code):
            srv.outcome = ("bad", None)
            self._reply(400, _NOT_FOUND)
            return
        srv.outcome = ("code", code)
        self._reply(200, _PAGE, "text/html; charset=utf-8")

    def _no(self) -> None:
        srv: _Receiver = self.server  # type: ignore[assignment]
        srv.seen += 1
        self._reply(405, _NOT_FOUND)

    do_POST = do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _no  # type: ignore[assignment]


class _Receiver(http.server.HTTPServer):
    """One-shot listener on 127.0.0.1 for the browser's redirect.

    It binds loopback only, on a random port, answers GET on exactly one secret
    path, checks the Host header, compares the state in constant time, accepts
    one valid request and is closed by the caller straight afterwards.
    """

    allow_reuse_address = False
    timeout = 0.5

    def __init__(self) -> None:
        super().__init__((LOOPBACK_HOST, 0), _Handler)
        self.path_token = secrets.token_urlsafe(24)
        self.state = secrets.token_urlsafe(24)
        self.outcome: tuple[str, str | None] | None = None
        self.seen = 0
        port = self.server_address[1]
        # OpenRouter's docs name localhost callbacks on any port; the browser may
        # dial either name, and the Host header it sends is what we check.
        self.allowed_hosts = {f"localhost:{port}", f"127.0.0.1:{port}"}

    def server_bind(self) -> None:
        # HTTPServer.server_bind also does a reverse-DNS lookup of the address, which can stall; skip it.
        socketserver.TCPServer.server_bind(self)
        self.server_name = LOOPBACK_HOST
        self.server_port = self.server_address[1]

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def callback_url(self) -> str:
        return f"http://localhost:{self.port}{CALLBACK_PREFIX}{self.path_token}"

    def wait(self, timeout_s: float, monotonic: Callable[[], float], tick: Callable[[], None] | None) -> tuple[str, str | None]:
        """Serve until a decisive request arrives. Returns (kind, code): code | denied | bad | timeout | flood."""
        deadline = monotonic() + timeout_s
        while self.outcome is None:
            if monotonic() >= deadline:
                return ("timeout", None)
            if self.seen > MAX_REJECTED:
                return ("flood", None)
            if tick is not None:
                tick()
            self.handle_request()
        return self.outcome

    def handle_error(self, request: Any, client_address: Any) -> None:  # noqa: ARG002
        pass  # the default prints the request; keep it silent


# ------------------------------------------------------------- the exchange


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:  # noqa: ARG002
        return None  # a redirect on the exchange is refused, never followed


def _opener(insecure_ok: bool) -> urllib.request.OpenerDirector:
    if insecure_ok:
        # Tests only (Endpoints.allow_insecure): talk to a local fake, bypassing any proxy variables.
        return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect)
    context = ssl.create_default_context()  # verifies the certificate and the host name
    return urllib.request.build_opener(_NoRedirect, urllib.request.HTTPSHandler(context=context))


def exchange_code(
    endpoints: Endpoints,
    code: str,
    verifier: str,
    *,
    timeout: float = 15.0,
    lang: str = "en",
) -> str:
    """Trade the one-time code (plus the PKCE verifier) for the provider's key.

    HTTPS with certificate checks, no redirects, a bounded reply. On any failure
    the error says why in general terms; the reply body is never shown.
    """
    endpoints.check()
    body = json.dumps({"code": code, "code_verifier": verifier, "code_challenge_method": "S256"}).encode("utf-8")
    request = urllib.request.Request(
        endpoints.exchange_url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json", "User-Agent": f"muse/{__version__}"},
    )
    opener = _opener(endpoints.allow_insecure and not endpoints.exchange_url.startswith("https://"))
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - scheme checked above
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_BODY:
                raise ConnectError(ct("reason_size", lang))
            raw = response.read(MAX_BODY + 1)
    except ConnectError:
        raise
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        if 300 <= status < 400:
            raise ConnectError(ct("reason_redirect", lang)) from None
        raise ConnectError(ct("reason_http", lang, status=status)) from None
    except urllib.error.URLError as exc:
        if isinstance(exc.reason, ssl.SSLError):
            raise ConnectError(ct("reason_tls", lang)) from None
        raise ConnectError(ct("reason_network", lang, kind=type(exc.reason).__name__)) from None
    except (OSError, ValueError) as exc:
        raise ConnectError(ct("reason_network", lang, kind=type(exc).__name__)) from None
    if len(raw) > MAX_BODY:
        raise ConnectError(ct("reason_size", lang))
    try:
        value = json.loads(raw.decode("utf-8")).get("key")
    except (ValueError, AttributeError, UnicodeDecodeError):
        raise ConnectError(ct("reason_shape", lang)) from None
    if not isinstance(value, str) or not 8 <= len(value) <= 512 or not value.isprintable() or any(c.isspace() for c in value):
        raise ConnectError(ct("reason_shape", lang))
    return value


# ---------------------------------------------------------------- OAuth flow


def _openrouter_caveats(provider: Provider, io: ConnectIO, lang: str) -> None:
    """The two honest caveats from the plan, with the numbers taken from the catalogue."""
    small = next((a for a in provider.allowance if a.window == "day" and a.condition and "fewer" in a.condition), None)
    big = next((a for a in provider.allowance if a.window == "day" and a is not small and a.condition), None)
    if small and big:
        io.say(ct("oa_caveat_small", lang, small=catalog.fmt_number(small.limit), big=catalog.fmt_number(big.limit)))
    else:
        io.say(ct("oa_caveat_small_plain", lang))
    io.say(ct("oa_caveat_learn", lang))


def _show_probe(io: ConnectIO, key: str, lang: str) -> None:
    """Ask OpenRouter what is left, using the existing probe. Best effort."""
    probe = io.probe
    if probe is None:
        from muse import probes

        probe = probes.probe_openrouter
    try:
        limits = probe(key)
    except Exception:
        limits = []
    day = next((x for x in limits if x.get("window") == "day" and x.get("metric") == "requests"), None)
    if day and day.get("remaining") is not None and day.get("limit") is not None:
        io.say(ct("oa_probe", lang, remaining=catalog.fmt_number(day["remaining"]), limit=catalog.fmt_number(day["limit"])))
    else:
        io.say(ct("oa_probe_unknown", lang))


def _store(io: ConnectIO, provider: Provider, key: str, lang: str, env: Mapping[str, str]) -> None:
    try:
        result = keys.set_key(str(provider.env), key)
    except keys.KeysError as exc:
        raise ConnectError(str(exc)) from None  # KeysError messages never contain values
    for warning in result.warnings:
        io.warn(f"warning: {warning}")
    io.say(ct("saved", lang, location=result.location))


def connect_oauth(
    io: ConnectIO,
    env: Mapping[str, str],
    provider: Provider,
    lang: str,
    *,
    no_browser: bool = False,
    paste_code: bool = False,
    timeout_s: float = DEFAULT_WAIT_S,
    endpoints: Endpoints | None = None,
) -> int:
    """One-tap connection (OAuth PKCE). Returns a process exit code."""
    conn = provider.connection
    if conn is None or conn.method != "oauth_pkce" or not conn.auth_url or not conn.exchange_url or not provider.env:
        raise ConnectError(ct("unknown_helper", lang))
    ep = endpoints or Endpoints(conn.auth_url, conn.exchange_url)
    ep.check()
    name = provider.name
    io.say(ct("oa_title", lang, name=name))
    io.say(ct("oa_intro", lang, name=name))
    io.say()
    verifier, challenge = pkce_pair()
    receiver: _Receiver | None = None
    try:
        if paste_code:
            url = build_auth_url(ep.auth_url, challenge, callback_url=None, state=None)
            io.say(ct("oa_paste_intro", lang, name=name, url=url))
            if io.secret is None:
                raise ConnectError(ct("no_terminal", lang, id=provider.id))
            code = (io.secret(ct("oa_paste_prompt", lang)) or "").strip()
            if not code:
                io.say(ct("oa_paste_empty", lang))
                return 1
        else:
            try:
                receiver = _Receiver()
            except OSError as exc:
                raise ConnectError(ct("oa_bind_failed", lang, kind=type(exc).__name__)) from None
            url = build_auth_url(ep.auth_url, challenge, callback_url=receiver.callback_url, state=receiver.state)
            opened = False
            if not no_browser:
                try:
                    opened = bool(io.open_url(url))
                except Exception:
                    opened = False
            if no_browser or not opened:
                io.say(ct("oa_url", lang, url=url))
            io.say(ct("oa_wait", lang, minutes=max(1, int(timeout_s // 60)) if timeout_s >= 60 else 1))
            try:
                kind, code_or_none = receiver.wait(timeout_s, io.monotonic, io.tick)
            except KeyboardInterrupt:
                raise Cancelled(ct("oa_cancelled", lang)) from None
            finally:
                receiver.server_close()  # stop listening the moment we have an answer or give up
            receiver = None
            if kind == "timeout":
                raise ConnectError(ct("oa_timeout", lang))
            if kind == "denied":
                raise Cancelled(ct("oa_denied", lang, name=name))
            if kind == "flood":
                raise ConnectError(ct("oa_too_many", lang))
            if kind != "code" or not code_or_none:
                raise ConnectError(ct("oa_bad_callback", lang))
            code = code_or_none
    finally:
        if receiver is not None:
            receiver.server_close()
    try:
        key = exchange_code(ep, code, verifier, lang=lang)
    except ConnectError as exc:
        raise ConnectError(ct("oa_exchange_failed", lang, name=name, reason=str(exc), env=provider.env)) from None
    finally:
        code = ""  # one-time and spent either way
    _store(io, provider, key, lang, env)
    _show_probe(io, key, lang)
    key = ""
    io.say(ct("gained", lang, gain=_gain_for(provider, lang)))
    if provider.id == OPENROUTER:
        _openrouter_caveats(provider, io, lang)
    io.say()
    after_connect(io, env, lang, provider.id)
    return 0


# ------------------------------------------------------------ paste-key flow


def _default_http_get(url: str, headers: dict[str, str], timeout: float) -> int:
    """One GET over verified https, no redirects. Returns the status; the body is never read or shown."""
    if not url.startswith("https://"):
        raise ConnectError("test url is not https")
    request = urllib.request.Request(url, headers={**headers, "User-Agent": f"muse/{__version__}", "Accept": "application/json"}, method="GET")
    opener = _opener(False)
    try:
        with opener.open(request, timeout=timeout) as response:  # noqa: S310 - https checked above
            return int(response.status)
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        return int(status)


def run_key_test(io: ConnectIO, provider: Provider, key: str, lang: str) -> None:
    """With --test only: ONE request that checks the key and sends no prompt."""
    conn = provider.connection
    spec = conn.test if conn else None
    if not spec:
        io.say(ct("test_none", lang, name=provider.name))
        return
    url = str(spec["url"])
    auth = spec.get("auth")
    headers: dict[str, str] = {}
    if auth == "bearer":
        headers["Authorization"] = f"Bearer {key}"
        auth_text = ct("test_auth_bearer", lang)
    else:
        header_name = str((auth or {}).get("header"))
        headers[header_name] = key
        auth_text = ct("test_auth_header", lang, name=header_name)
    io.say(ct("test_intro", lang, url=url, auth=auth_text))
    getter = io.http_get or _default_http_get
    try:
        status = int(getter(url, headers, 10.0))
    except ConnectError:
        io.warn(ct("test_failed", lang, name=provider.name, kind="not https"))
        return
    except Exception as exc:  # network failure: say the kind, never the text (it could echo a header)
        io.warn(ct("test_failed", lang, name=provider.name, kind=type(exc).__name__))
        return
    if 200 <= status < 300:
        io.say(ct("test_ok", lang, name=provider.name))
    elif status in (401, 403):
        io.warn(ct("test_rejected", lang, name=provider.name, status=status))
    elif status == 429:
        io.say(ct("test_limited", lang, name=provider.name))
    else:
        io.warn(ct("test_other", lang, name=provider.name, status=status))


def connect_paste_key(
    io: ConnectIO,
    env: Mapping[str, str],
    provider: Provider,
    lang: str,
    *,
    test: bool = False,
    open_page: bool | None = None,
) -> int:
    """Guided paste-a-key flow from catalogue data. Returns a process exit code."""
    conn = provider.connection
    if conn is None or conn.method != "paste_key" or not provider.env:
        raise ConnectError(ct("unknown_helper", lang))
    name = provider.name
    io.say(ct("guide_title", lang, name=name))
    io.say(ct("guide_only", lang, name=name))
    io.say(ct("guide_gain", lang, gain=_gain_for(provider, lang)))
    friction = catalog.friction_text(conn.card_required, conn.phone_required, lang)
    if friction:
        io.say(ct("guide_friction", lang, friction=friction))
    io.say()
    io.say(ct("guide_steps", lang))
    for number, step in enumerate(conn.steps, start=1):
        io.say(f"  {number}. {catalog.pick(step.title, lang)}")
        io.say(f"     {catalog.pick(step.detail, lang)}")
    io.say()

    want_open = open_page
    if want_open is None:
        want_open = bool(io.confirm(ct("ask_open", lang, name=name), False)) if io.confirm else False
    if want_open and conn.key_url:
        io.say(ct("opening", lang, url=conn.key_url))
        try:
            ok = bool(io.open_url(conn.key_url))
        except Exception:
            ok = False
        if not ok:
            io.say(ct("open_yourself", lang, url=conn.key_url))
    elif conn.key_url:
        io.say(ct("open_yourself", lang, url=conn.key_url))
    io.say()

    if io.secret is None:
        io.warn(ct("no_terminal", lang, id=provider.id))
        return 2
    raw = io.secret(ct("prompt_key", lang, name=name))
    try:
        value = keys.clean_value(raw or "")
    except keys.KeysError:
        io.say(ct("empty_value", lang))
        return 1
    finally:
        raw = ""
    prefix = keys.expected_prefix(str(provider.env))
    if prefix and not value.startswith(prefix):
        if not (io.confirm and io.confirm(ct("shape_ask", lang, name=name, prefix=prefix), False)):
            io.say(ct("declined", lang))
            return 1
    extras: list[tuple[str, str]] = []
    for extra in provider.also_env:
        if io.text is None:
            io.warn(ct("no_terminal", lang, id=provider.id))
            return 2
        got = (io.text(ct("prompt_extra", lang, label=extra)) or "").strip()
        try:
            extras.append((extra, keys.clean_value(got)))
        except keys.KeysError:
            io.say(ct("empty_value", lang))
            return 1
    try:
        result = keys.set_key(str(provider.env), value)
        for extra, extra_value in extras:
            extra_result = keys.set_key(extra, extra_value)
            io.say(ct("saved_extra", lang, label=extra, location=extra_result.location))
    except keys.KeysError as exc:
        io.warn(ct("bad_value", lang, problem=str(exc)))
        return 1
    for warning in result.warnings:
        io.warn(f"warning: {warning}")
    io.say(ct("saved", lang, location=result.location))
    if test:
        run_key_test(io, provider, value, lang)
    else:
        io.say(ct("test_hint", lang))
    value = ""
    io.say(ct("gained", lang, gain=_gain_for(provider, lang)))
    io.say()
    after_connect(io, env, lang, provider.id)
    return 0


# ----------------------------------------------------------------- local


def connect_local(io: ConnectIO, env: Mapping[str, str], lang: str, *, write: bool = True) -> int:
    """Find models on this machine (Ollama, LM Studio, OpenAI-compatible servers) and catalogue them."""
    io.say(ct("local_title", lang))
    probes_ = local.probe_runtimes(env)
    for probe in probes_:
        mark = "ok" if probe.reachable else "--"
        extra = f"({len(probe.models)} models)" if probe.reachable else f"({probe.error})"
        io.say(f"  [{mark}] {probe.provider:<13} {probe.base}  {extra}")
    found = local.discover(env)
    count = sum(len(v) for v in found.values())
    if not count:
        io.say(ct("local_none", lang))
        return 1
    io.say(ct("local_found", lang, n=count))
    if not write:
        io.say(ct("local_dry", lang))
        return 0
    target = local.write_catalog(found, paths.local_catalog_path(env))
    io.say(ct("local_wrote", lang, path=target))
    for var, entry in local.suggest_env(found, env).items():
        io.say(f"  {var}   host: {entry['host']}   docker: {entry['docker']}")
    io.say()
    after_connect(io, env, lang, None)
    return 0


# ---------------------------------------------------------------- resources


def resources_payload(cat: catalog.Catalog) -> dict[str, Any]:
    return {
        "resources": [catalog.resource_to_dict(r) for r in cat.resources],
        "subscriptions": [catalog.subscription_to_dict(s) for s in cat.subscriptions],
    }


def run_resources(io: ConnectIO, lang: str, *, as_json: bool = False) -> int:
    """Read-only: what is free outside AI, when it refills, how it would connect, and the hand-off subscriptions."""
    cat = _load()
    if as_json:
        io.say(json.dumps(resources_payload(cat), indent=2, ensure_ascii=False))
        return 0
    io.say(ct("res_title", lang))
    io.say(ct("res_sub", lang))
    if not cat.resources:
        io.say(ct("res_none", lang))
    for kind in ("storage", "compute", "chain"):
        group = [r for r in cat.resources if r.kind == kind]
        if not group:
            continue
        io.say()
        io.say(ct(f"res_kind_{kind}", lang))
        for res in group:
            head = res.headline()
            line = f"  - {catalog.pick(res.name, lang)}: "
            line += catalog.allowance_text(head, lang) if head else catalog.tr("gain_unknown", lang)
            line += f" · {catalog.reset_text(res.reset, lang)}"
            io.say(line + f" [{ct('res_tag', lang)}]")
            if res.connection and res.connection.how:
                io.say("      " + ct("res_how", lang, how=catalog.pick(res.connection.how, lang)))
            io.say("      " + ct("res_checked", lang, date=res.checked_on or "?", conf=res.confidence or "?", conf_text=catalog.tr(f"conf_{res.confidence}", lang) if res.confidence else "?"))
    io.say()
    io.say(ct("sub_title", lang))
    io.say(ct("sub_sub", lang))
    for sub in cat.subscriptions:
        web = sub.open.get("web") or {}
        app = sub.open.get("app") or {}
        io.say(f"  - {sub.name}: {catalog.pick(sub.description, lang)} [{ct('sub_tag', lang)}]")
        if web.get("url"):
            io.say("      " + ct("sub_open_web", lang, url=web["url"]))
        if app.get("url"):
            io.say("      " + ct("sub_open_app", lang, url=app["url"]))
        documented = app.get("prefill") == "documented"
        io.say("      " + ct("sub_prefill_yes" if documented else "sub_prefill_no", lang))
    return 0


# ------------------------------------------------------------------ entry


def next_helper_line(snapshot: Mapping[str, Any], lang: str) -> str | None:
    """The one-line "Next helper to add: ..." for `muse status`, from a snapshot. None when there is nothing to add."""
    for item in snapshot.get("suggestions") or []:
        if item.get("id") == "local":
            continue
        text = (item.get("text") or {}).get(lang) or (item.get("text") or {}).get("en") or {}
        meta = text.get("meta")
        name = item.get("name")
        if not name:
            continue
        return tr("status_next", lang, name=name, meta=meta) if meta else tr("status_next_plain", lang, name=name)
    return None


def run_connect(
    io: ConnectIO,
    env: Mapping[str, str],
    *,
    provider: str | None,
    extra: list[str] | None = None,
    lang: str | None = None,
    no_browser: bool = False,
    paste_code: bool = False,
    test: bool = False,
    open_page: bool | None = None,
    timeout_s: float = DEFAULT_WAIT_S,
    write_local: bool = True,
    as_json: bool = False,
    endpoints: Endpoints | None = None,
) -> int:
    """`muse connect [helper]`. Returns an exit code: 0 done, 1 not saved, 2 usage."""
    lang = catalog.normalise_lang(lang) if lang else catalog.default_lang(env)
    # A key must never travel on the command line: refuse anything that is not a plain helper name,
    # and never echo it back.
    if extra or (provider is not None and not looks_like_name(provider)):
        io.warn(ct("not_a_name", lang))
        return 2
    try:
        if provider is None:
            return show_overview(io, env, lang, as_json=as_json)
        if provider == "local":
            return connect_local(io, env, lang, write=write_local)
        cat = _load()
        found = find_provider(cat.providers, provider)
        if found is None or found.connection is None:
            io.warn(ct("unknown_helper", lang))
            return 2
        if found.connection.method == "oauth_pkce":
            return connect_oauth(io, env, found, lang, no_browser=no_browser, paste_code=paste_code, timeout_s=timeout_s, endpoints=endpoints)
        if found.connection.method == "paste_key":
            return connect_paste_key(io, env, found, lang, test=test, open_page=open_page)
        io.warn(ct("unknown_helper", lang))
        return 2
    except Cancelled as exc:
        io.say(str(exc))
        return 1
    except ConnectError as exc:
        io.warn(str(exc))
        return 1
