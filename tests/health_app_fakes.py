"""In-memory stand-ins for the vault, mandate book, receipt log and bot, so the
web app can be built and tested without the real implementations."""

from __future__ import annotations

import hashlib
import itertools
import json
import os
from datetime import datetime, timezone
from typing import Any

from muse.health.app.services import ImportedDoc, NotSupported, Runtime, Services
from muse.health.contracts import (
    BotReply, Item, Mandate, ProposedChange, Receipt, Record, Tenant, VaultLocked, VerifyResult, WrongPassphrase,
)
from muse.health.tenant import load_tenant


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakeStore:
    """Shared backing state; every FakeVault instance opened on it sees the same data."""

    def __init__(self) -> None:
        self.exists = False
        self.passphrase = ""
        self.records: dict[str, tuple[Record, str]] = {}
        self.items: dict[str, Item] = {}
        self.mandates: list[Mandate] = []
        self.receipts: list[Receipt] = []
        self.queue: list[BotReply] = []          # scripted replies served first by the bot
        self.pending: dict[str, ProposedChange] = {}
        self.seq = itertools.count(1)
        self.ids = itertools.count(1)
        self.last_archive: bytes = b""
        self.chat_calls: list[tuple[str, list[dict[str, str]]]] = []
        self.vaults: list[FakeVault] = []

    def nid(self, prefix: str) -> str:
        return f"{prefix}_{next(self.ids):04d}"


class FakeVault:
    def __init__(self, store: FakeStore) -> None:
        self.s = store
        self._unlocked = False
        store.vaults.append(self)

    def _need(self) -> None:
        if not self._unlocked:
            raise VaultLocked("locked")

    def is_unlocked(self) -> bool:
        return self._unlocked

    def unlock(self, passphrase: str) -> None:
        if passphrase != self.s.passphrase:
            raise WrongPassphrase("wrong")
        self._unlocked = True

    def lock(self) -> None:
        self._unlocked = False

    def add(self, kind: str, title: str, text: str, occurred_on: str | None = None, source: str = "") -> Record:
        self._need()
        rec = Record(self.s.nid("rec"), kind, title, occurred_on, source, now_iso(), len(text),
                     hashlib.sha256(text.encode()).hexdigest())
        self.s.records[rec.id] = (rec, text)
        return rec

    def get(self, record_id: str) -> tuple[Record, str]:
        self._need()
        return self.s.records[record_id]

    def list(self, kind: str | None = None, since: str | None = None, until: str | None = None, limit: int = 200) -> list[Record]:
        self._need()
        rows = [r for r, _ in self.s.records.values() if kind in (None, r.kind)]
        rows.sort(key=lambda r: (r.occurred_on or r.added_at), reverse=True)
        return rows[:limit]

    def search(self, query: str, limit: int = 8) -> list[tuple[Record, str]]:
        self._need()
        return [(r, t[:60]) for r, t in self.s.records.values() if query in t or query in r.title][:limit]

    def delete(self, record_id: str) -> None:
        self._need()
        del self.s.records[record_id]

    def add_item(self, kind: str, data: dict[str, Any]) -> Item:
        self._need()
        it = Item(self.s.nid("item"), kind, dict(data), now_iso(), now_iso())
        self.s.items[it.id] = it
        return it

    def list_items(self, kind: str | None = None) -> list[Item]:
        self._need()
        return [i for i in self.s.items.values() if kind in (None, i.kind)]

    def update_item(self, item_id: str, data: dict[str, Any]) -> Item:
        self._need()
        old = self.s.items[item_id]
        it = Item(old.id, old.kind, {**old.data, **data}, old.created_at, now_iso())
        self.s.items[item_id] = it
        return it

    def delete_item(self, item_id: str) -> None:
        self._need()
        del self.s.items[item_id]

    def sign(self, message: bytes) -> bytes:
        self._need()
        return hashlib.sha256(b"fake-key" + message).digest() * 2

    def public_key(self) -> bytes:
        return b"\x01" * 32

    def export_archive(self, dest: str) -> None:
        self._need()
        blob = b"MUSE-FAKE-ARCHIVE\x00" + os.urandom(96)   # opaque: carries no record text
        self.s.last_archive = blob
        with open(dest, "wb") as f:
            f.write(blob)


class FakeMandateBook:
    def __init__(self, store: FakeStore) -> None:
        self.s = store

    def grant(self, grantee: str, scopes: tuple[str, ...], purpose: str, record_kinds: tuple[str, ...] = (), expires_at: str | None = None) -> Mandate:
        m = Mandate(self.s.nid("mnd"), grantee, tuple(scopes), purpose, tuple(record_kinds), now_iso(), expires_at, None, "c2ln")
        self.s.mandates.append(m)
        return m

    def revoke(self, mandate_id: str) -> None:
        for i, m in enumerate(self.s.mandates):
            if m.id == mandate_id:
                self.s.mandates[i] = Mandate(m.id, m.grantee, m.scopes, m.purpose, m.record_kinds, m.created_at,
                                             m.expires_at, now_iso(), m.signature)
                return
        raise KeyError(mandate_id)

    def all(self) -> list[Mandate]:
        return list(self.s.mandates)

    def check(self, grantee, action):  # pragma: no cover - not used by the web layer
        raise NotImplementedError


def _rhash(prev: str, r: dict) -> str:
    return hashlib.sha256((prev + json.dumps(r, sort_keys=True)).encode()).hexdigest()


class FakeReceiptLog:
    def __init__(self, store: FakeStore) -> None:
        self.s = store

    def append(self, actor: str, action: str, *, mandate_id: str | None = None, record_refs: tuple[str, ...] = (),
               egress: str = "none", model: str | None = None, privacy: str | None = None, detail: str = "") -> Receipt:
        prev = self.s.receipts[-1].hash if self.s.receipts else "0" * 64
        body = dict(seq=next(self.s.seq), ts=now_iso(), actor=actor, action=action, mandate_id=mandate_id,
                    record_refs=list(record_refs), egress=egress, model=model, privacy=privacy, detail=detail)
        r = Receipt(body["seq"], body["ts"], actor, action, mandate_id, tuple(record_refs), egress, model, privacy,
                    detail, prev, _rhash(prev, body), "c2ln")
        self.s.receipts.append(r)
        return r

    def list(self, limit: int = 50, before_seq: int | None = None) -> list[Receipt]:
        rows = [r for r in self.s.receipts if before_seq is None or r.seq < before_seq]
        return list(reversed(rows))[:limit]

    def verify(self) -> VerifyResult:
        prev = "0" * 64
        for r in self.s.receipts:
            body = dict(seq=r.seq, ts=r.ts, actor=r.actor, action=r.action, mandate_id=r.mandate_id,
                        record_refs=list(r.record_refs), egress=r.egress, model=r.model, privacy=r.privacy, detail=r.detail)
            if r.prev_hash != prev or r.hash != _rhash(prev, body):
                return VerifyResult(False, len(self.s.receipts), r.seq, "the chain does not match at this entry")
            prev = r.hash
        return VerifyResult(True, len(self.s.receipts), None, "every entry matches")

    def tamper(self, seq: int, detail: str = "edited") -> None:
        for i, r in enumerate(self.s.receipts):
            if r.seq == seq:
                self.s.receipts[i] = Receipt(**{**r.__dict__, "detail": detail})


class ScriptedBot:
    """Replies by keyword so every UI state can be shown and tested."""

    def __init__(self, vault: FakeVault, mandates: FakeMandateBook, receipts: FakeReceiptLog, tenant: Tenant, store: FakeStore):
        self.v, self.m, self.r, self.t, self.s = vault, mandates, receipts, tenant, store

    def chat(self, user_text: str, history: list[dict[str, str]]) -> BotReply:
        self.s.chat_calls.append((user_text, history))
        if self.s.queue:
            return self.s.queue.pop(0)
        low = user_text.lower()
        if any(k in low for k in ("胸痛", "喘不過氣", "chest pain", "urgent")):
            r = self.r.append("bot", "safety.urgent", detail="urgent wording; no model call")
            return BotReply("你描述的狀況可能很緊急。請先不要等待回覆，立刻求助。", safety="urgent", receipts=(r.seq,))
        if any(k in low for k in ("該不該", "可以停藥", "要不要吃", "should i stop", "increase my dose")):
            r = self.r.append("bot", "safety.declined", detail="medical advice declined")
            return BotReply("要不要調整藥物，需要醫師看過你的整體狀況才能決定。我可以幫你整理這次想問醫師的重點。",
                            safety="declined_medical_advice", receipts=(r.seq,))
        if any(k in low for k in ("nomodel", "無模型", "no model")):
            r = self.r.append("bot", "llm.refused", detail="refused: no local model")
            return BotReply("目前沒有可用的院內模型，我沒有讀取你的紀錄。", safety="no_model", receipts=(r.seq,))
        if any(k in low for k in ("提醒我", "新增提醒", "remind me")):
            pc = ProposedChange(self.s.nid("chg"), "add_item", "新增提醒：11 月 18 日 領慢性處方箋",
                                {"kind": "reminder", "data": {"title": "領慢性處方箋", "date": "2026-11-18"}})
            self.s.pending[pc.id] = pc
            return BotReply("好的，我準備了一則提醒，請你確認後才會儲存。", proposed=(pc,))
        recs = self.v.list(limit=3)
        refs = tuple(r.content_sha256 for r in recs)
        rd = self.r.append("bot", "vault.read", record_refs=refs, egress="none", detail=f"{len(recs)} records")
        lm = self.r.append("bot", "llm.complete", record_refs=refs, egress="local", model="hospital/qwen2.5-72b",
                           privacy="local", detail="answer")
        return BotReply("根據你最近的紀錄：糖化血色素 6.8%，比上次稍微下降；低密度膽固醇 128 mg/dL，略高於一般建議值。"
                        "詳細意義請在回診時和醫師討論。", cited_records=tuple(r.id for r in recs),
                        receipts=(rd.seq, lm.seq), route="hospital/qwen2.5-72b (local)")

    def confirm(self, change_id: str) -> Any:
        pc = self.s.pending.pop(change_id)           # KeyError -> 404
        if pc.kind != "add_item":
            raise PermissionError("not allowed")
        self.v.add_item(pc.payload["kind"], pc.payload["data"])
        r = self.r.append("bot", "items.write", detail="1 reminder added")
        return BotReply("已儲存：" + pc.summary, receipts=(r.seq,))

    def reject(self, change_id: str) -> Any:
        pc = self.s.pending.pop(change_id)
        return BotReply("已取消，沒有儲存：" + pc.summary)


def fake_importer(filename: str, data: bytes) -> list[ImportedDoc]:
    text = data.decode("utf-8", "replace")
    if "BAD-FILE" in text:
        raise ValueError("cannot read")
    ext = filename.rsplit(".", 1)[-1].lower()
    if ext == "json":
        rows = json.loads(text)
        return [ImportedDoc(d.get("kind", "document"), d["title"], d["text"], d.get("occurred_on"), d.get("source", filename))
                for d in rows]
    first = text.strip().splitlines()[0][:80] if text.strip() else filename
    return [ImportedDoc("document", first, text, None, filename)]


def default_spec(book: FakeMandateBook) -> Mandate:
    return book.grant("bot", ("vault.read", "llm.local", "items.write", "draft.letter"),
                      "回答我關於自己紀錄的問題、整理提醒與看診問題")



def make_fake_services(tenant: Tenant | None = None, *, passphrase: str | None = None, seed: bool = False) -> tuple[Services, FakeStore]:
    """passphrase given -> a vault already exists (optionally seeded with demo data)."""
    tenant = tenant or load_tenant("demo-hospital")
    store = FakeStore()

    def vault_exists() -> bool:
        return store.exists

    def create_vault(pw: str) -> FakeVault:
        store.exists, store.passphrase = True, pw
        v = FakeVault(store)
        v._unlocked = True
        return v

    def open_vault() -> FakeVault:
        return FakeVault(store)

    def make_runtime(v: FakeVault) -> Runtime:
        m, r = FakeMandateBook(store), FakeReceiptLog(store)
        return Runtime(v, m, r, ScriptedBot(v, m, r, tenant, store))

    def change_passphrase(v: FakeVault, old: str, new: str) -> None:
        if old != store.passphrase:
            raise WrongPassphrase("wrong")
        store.passphrase = new

    def destroy(vault=None) -> None:
        store.__init__()  # type: ignore[misc]

    svc = Services(tenant, vault_exists, create_vault, open_vault, make_runtime, fake_importer, default_spec,
                   change_passphrase, destroy, version="test")
    if passphrase is not None:
        v = create_vault(passphrase)
        rt = make_runtime(v)
        rt.receipts.append("user", "consent.accept", detail=f"consent {tenant.consent_version}")
        mm = default_spec(rt.mandates)
        rt.receipts.append("user", "mandate.grant", mandate_id=mm.id, detail="default assistant mandate")
        if seed:
            seed_demo(rt)
        v.lock()
    return svc, store


def seed_demo(rt: Runtime) -> None:
    v = rt.vault
    v.add("lab", "糖化血色素與血脂檢驗", "檢驗日期：2026-10-08\n糖化血色素 HbA1c：6.8 %（參考值 4.0–5.6）\n空腹血糖：118 mg/dL（參考值 70–99）\n"
          "總膽固醇：205 mg/dL\n低密度脂蛋白 LDL-C：128 mg/dL（建議 < 100）\n高密度脂蛋白 HDL-C：52 mg/dL\n三酸甘油酯：146 mg/dL",
          "2026-10-08", "示範醫院 檢驗科")
    v.add("lab", "血液常規與腎功能", "白血球 6.2 x10^3/uL；血紅素 13.9 g/dL；血小板 238 x10^3/uL\n肌酸酐 0.92 mg/dL；eGFR 84 mL/min/1.73m2\n尿酸 6.9 mg/dL（偏高）",
          "2026-07-15", "示範醫院 檢驗科")
    v.add("lab", "年度健康檢查血液報告", "空腹血糖 109 mg/dL；糖化血色素 7.1 %；LDL-C 141 mg/dL；肝功能 AST 24 / ALT 28 U/L 正常。",
          "2025-11-03", "示範醫院 健康管理中心")
    v.add("imaging_report", "胸部 X 光報告", "檢查日期：2026-03-21\n影像所見：雙側肺野清晰，無明顯浸潤或積液；心臟大小在正常範圍；橫膈及肋膜角清楚。\n"
          "印象：目前影像無明顯急性異常。", "2026-03-21", "示範醫院 放射科")
    v.add("medication_list", "目前用藥清單（心臟內科）", "1. Amlodipine 5 mg　每天早上 1 顆\n2. Metformin 500 mg　早晚飯後各 1 顆\n3. Atorvastatin 10 mg　睡前 1 顆",
          "2026-09-12", "示範醫院 心臟內科")
    v.add("visit", "心臟內科門診紀錄", "血壓 132/84 mmHg，心跳 72。醫囑：持續規律服藥，每日自行量血壓，三個月後回診並抽血。",
          "2026-09-12", "示範醫院 心臟內科")
    v.add("note", "我的血壓記錄（自己整理）", "10/01 早 128/80；10/05 早 135/85；10/09 早 130/82；10/14 早 126/79",
          "2026-10-14", "自己")
    it = v.add_item
    it("medication", {"name": "Amlodipine 5 mg", "dose": "每天早上 1 顆", "schedule": "08:00", "note": "飯後"})
    it("medication", {"name": "Metformin 500 mg", "dose": "早晚飯後各 1 顆", "schedule": "08:30、19:00"})
    it("medication", {"name": "Atorvastatin 10 mg", "dose": "睡前 1 顆", "schedule": "22:00"})
    it("appointment", {"title": "心臟內科回診", "date": "2026-11-20", "time": "14:00", "place": "示範醫院 3 樓 305 診", "note": "帶血壓記錄"})
    it("appointment", {"title": "抽血（空腹 8 小時）", "date": "2026-12-02", "time": "08:30", "place": "示範醫院 檢驗科"})
    it("reminder", {"title": "領慢性病連續處方箋", "date": "2026-11-18", "time": "10:00"})
    it("reminder", {"title": "每天早上量血壓並記下來", "date": "", "time": "07:30"})
    it("question_list", {"title": "看診提問清單", "questions": ["LDL 還是偏高，需要調整嗎？", "手腳偶爾麻麻的，和糖尿病有關嗎？"]})
    rt.receipts.append("bot", "vault.read", record_refs=("a" * 64, "b" * 64, "c" * 64), detail="3 records", model=None)
    rt.receipts.append("bot", "llm.complete", record_refs=("a" * 64,), egress="local", model="hospital/qwen2.5-72b",
                       privacy="local", detail="answer")
