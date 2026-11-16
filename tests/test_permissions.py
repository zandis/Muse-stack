"""muse.permissions: the home's twin of core/src/permissions.rs.

Tests that sign or verify need the optional biscuit-python (extra `permissions`) and
skip without it; the rules for drafts, the plain-data forms and the "library missing"
behaviour run everywhere. The cross-language proof is core/conformance/permissions.json:
`test_committed_vectors_are_what_python_computes` here, and `cargo test` in core/.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import json
import os
import random
import re
import stat
import sys
from pathlib import Path

import pytest

from wardmuse import cli, keys
from wardmuse import permissions as P
from wardmuse.permissions import codec, store

ROOT = Path(__file__).resolve().parent.parent
needs_library = pytest.mark.skipif(not P.available(), reason="biscuit-python is not installed (pip install -e '.[permissions]')")

TEST_KEY = "ed25519-private/" + "01" * 32  # test-only; the key of the conformance fixtures
OTHER_KEY = "ed25519-private/" + "02" * 32
T0 = 1_794_528_000  # 2026-11-13T00:00:00Z
DAY = 86_400
NONE: dict = {}


def base_draft(issuer: str, **changes) -> P.Draft:
    fields = dict(
        issuer=issuer, holder="helper:vm", actions=("files.read", "files.write", "pay.spend"),
        resources=(P.Resource("folder", "/docs/work"), P.Resource("host", "api.example.com")),
        limits=(P.Limit("spend", "TWD", 2, "day", 2000), P.Limit("requests", "", 0, "day", 200)),
        purpose_text="Weekly digest 每週摘要", issued_at=T0, not_before=T0, not_after=T0 + 17 * DAY,
        confirm=P.EACH_WRITE, privacy="private", period_zone="Asia/Taipei")
    fields.update(changes)
    return P.Draft(**fields)


@pytest.fixture(scope="module")
def root():
    return P.parse_private_key(TEST_KEY)


@pytest.fixture(scope="module")
def pub(root) -> str:
    return P.public_key_text(root)


@pytest.fixture(scope="module")
def base(root, pub):
    return P.issue(base_draft(pub), root)


def req(action="files.read", resource=("folder", "/docs/work/a.md"), **kw) -> P.Request:
    kw.setdefault("holder", "helper:vm")
    kw.setdefault("privacy", "private")
    return P.Request(action=action, resource=P.Resource(*resource), **kw)


def decide(m, request, now=T0 + 5, usage=None, revoked=None, spent=None):
    usage = {("spend", "TWD", "day"): 0, ("requests", "", "day"): 0} if usage is None else usage
    return P.check(m, request, usage, revoked or P.RevocationList(), spent or P.SpentSet(), now)


# ---------------------------------------------------------------- without the library

ISSUER = "ed25519/" + "ab" * 32


def test_draft_rules_need_no_library():
    d = P.propose_from_fields(base_draft(ISSUER, actions=("pay.spend", "files.read"), resources=(P.Resource("host", "b"), P.Resource("folder", "/a"))))
    assert d.actions == ("files.read", "pay.spend") and d.resources[0].kind == "folder"
    assert P.mandate_id(d).startswith("m1_") and len(P.mandate_id(d)) == 67
    assert P.mandate_id(d) != P.mandate_id(dataclasses.replace(d, issued_at=T0 + 1)), "granting twice gives two ids"
    for changes, code in [
        (dict(holder="robot:x"), "invalid_holder"), (dict(actions=()), "invalid_actions"),
        (dict(resources=(P.Resource("folder", "/a/../b"),)), "invalid_resources"), (dict(purpose_text='say "hi"'), "invalid_purpose"),
        (dict(confirm=P.NEVER), "invalid_confirm"), (dict(privacy="secret"), "invalid_privacy"),
        (dict(not_after=T0), "invalid_validity"), (dict(limits=(P.Limit("spend", "TWD", 2, "day", True),)), "invalid_limits"),
        (dict(actions=("files.read", 7)), "invalid_actions"), (dict(purpose_text="\ud800"), "invalid_purpose"),
    ]:
        with pytest.raises(P.PermissionError) as err:
            P.propose_from_fields(base_draft(ISSUER, **changes))
        assert err.value.code == code, changes


def test_segment_aware_cover():
    work = P.Resource("folder", "/docs/work")
    assert work.covers(P.Resource("folder", "/docs/work")) and work.covers(P.Resource("folder", "/docs/work/a/b"))
    assert not work.covers(P.Resource("folder", "/docs/workshop")) and not work.covers(P.Resource("folder", "/docs"))
    assert not work.covers(P.Resource("folder", "*")) and P.Resource("folder", "*").covers(work)
    assert not P.Resource("host", "*").covers(work)
    assert work.intersect(P.Resource("folder", "/docs/work/sub")) == P.Resource("folder", "/docs/work/sub")
    assert work.intersect(P.Resource("folder", "/other")) is None


def test_plain_data_forms_round_trip():
    d = P.propose_from_fields(base_draft(ISSUER, confirm=P.Confirm("over_amount", "TWD", 2, 500)))
    assert codec.draft_from_data(json.loads(json.dumps(codec.draft_to_data(d)))) == d
    n = P.Narrowing(actions=("files.read",), resources=(P.Resource("folder", "/docs/work/x"),), limits=(P.NarrowLimit("spend", "TWD", "day", 5),),
                    caps=(P.Cap("spend", "TWD", 3),), not_before=T0, not_after=T0 + 9, privacy="local", confirm=P.EACH_WRITE, job="j", tool="t", card="c")
    assert codec.narrowing_from_data(json.loads(json.dumps(codec.narrowing_to_data(n)))) == n
    assert codec.narrowing_to_data(P.Narrowing()) == {}
    r = req("pay.spend", ("host", "api.example.com"), amounts=(P.Amount("spend", "TWD", 5),), card="c")
    assert codec.request_from_data(json.loads(json.dumps(codec.request_to_data(r)))) == r
    for bad in ({"actions": "files.read"}, {"surprise": 1}, {"resources": [["folder"]]}):
        with pytest.raises(P.PermissionError):
            codec.narrowing_from_data(bad)


def test_without_the_library_it_says_how_to_install(monkeypatch, capsys):
    monkeypatch.setitem(sys.modules, "biscuit_auth", None)  # makes `import biscuit_auth` fail
    assert P.available() is False
    with pytest.raises(P.PermissionError) as err:
        P.parse_private_key(TEST_KEY)
    assert err.value.code == "library_missing" and "biscuit-python==0.4.0" in err.value.detail
    with pytest.raises(P.PermissionError):
        P.check_encoded("token", ISSUER, req(), NONE, P.RevocationList(), P.SpentSet(), T0)
    monkeypatch.setitem(sys.modules, "keyring", None)
    assert cli.main(["permissions", "issue", "--holder", "helper:vm", "--action", "files.read", "--resource", "folder:/a", "--new-key", "--yes"]) == 1
    assert "biscuit-python" in capsys.readouterr().err


def test_pins_agree():
    """One Biscuit release on both sides: the Python binding wraps the crate version the core pins."""
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert f'permissions = ["biscuit-python=={P.BISCUIT_PYTHON_VERSION}"]' in pyproject
    cargo = (ROOT / "core" / "Cargo.toml").read_text(encoding="utf-8")
    assert re.search(r'^biscuit-auth = \{ version = "=6\.0\.0"', cargo, re.MULTILINE)
    lock = (ROOT / "core" / "Cargo.lock").read_text(encoding="utf-8")
    assert 'name = "biscuit-auth"\nversion = "6.0.0"' in lock


# ---------------------------------------------------------------- the vectors


def load_exporter():
    spec = importlib.util.spec_from_file_location("export_conformance", ROOT / "scripts" / "export_conformance.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@needs_library
def test_committed_vectors_are_what_python_computes():
    exporter = load_exporter()
    fresh = exporter.permissions_vectors().render()
    committed = (ROOT / "core" / "conformance" / "permissions.json").read_text(encoding="utf-8")
    assert fresh == committed, "core/conformance/permissions.json is out of date: run python scripts/export_conformance.py, then cargo test in core/"
    assert exporter.permissions_vectors().render() == fresh, "and the export is deterministic"


def test_vectors_cover_every_reason_and_rule():
    vectors = json.loads((ROOT / "core" / "conformance" / "permissions.json").read_text(encoding="utf-8"))
    assert len(vectors) >= 580
    decisions = {tuple(v["expected"]["ok"]) for v in vectors if v["fn"] == "permissions.check"}
    for v in vectors:
        if v["fn"] == "permissions.sequence":
            decisions |= {tuple(d) for d in v["expected"]["ok"]["decisions"]}
    reasons = {detail for kind, detail in decisions if kind == "deny"}
    assert reasons == {
        "malformed_token", "revoked", "bad_signature", "unsupported_schema", "invalid_mandate", "wrong_issuer", "unknown_block", "bad_request",
        "wrong_holder", "not_yet_valid", "expired", "action_not_allowed", "resource_not_allowed", "privacy_not_allowed", "wrong_job", "wrong_tool",
        "wrong_card", "single_use_too_long", "already_used", "amount_required", "limit_missing", "usage_missing", "over_limit", "over_cap",
        "spent_set_full", "engine_disagreement"}
    assert {detail for kind, detail in decisions if kind == "needs_confirmation"} == {"each_write", "over_amount"}
    assert ("allow", "") in decisions
    narrowed = [v for v in vectors if v["fn"] == "permissions.narrow"]
    kinds = set().union(*(set(step) for v in narrowed for step in v["input"]["narrowings"]))
    assert kinds == {"actions", "resources", "limits", "caps", "not_before", "not_after", "privacy", "confirm", "job", "tool", "card"}
    assert {v["expected"].get("err") for v in narrowed} >= {"not_narrower", "invalid_narrowing", "empty_narrowing", "sealed_token"}
    fixtures = json.loads((ROOT / "core" / "conformance" / "fixtures" / "permission_tokens.json").read_text(encoding="utf-8"))
    made_in_python = json.loads((ROOT / "core" / "conformance" / "fixtures" / "permission_tokens_py.json").read_text(encoding="utf-8"))
    read = {v["input"]["token"] for v in vectors if v["fn"] == "permissions.read"}
    assert set(fixtures["tokens"].values()) | set(made_in_python["tokens"].values()) <= read, "every fixture token is read by both sides"
    assert fixtures["root_private"] == TEST_KEY, "the fixtures are signed with the test-only key and nothing else is"


# ---------------------------------------------------------------- the flow


@needs_library
def test_issue_check_narrow_revoke(root, pub, base):
    assert base.id == P.mandate_id(base.draft) and len(P.revocation_ids(base)) == 1
    again = P.Mandate.parse(base.to_base64(), pub)
    assert again.draft == base.draft and again.id == base.id and P.revocation_ids(again) == P.revocation_ids(base)
    twin = P.issue(base_draft(pub), root)
    assert twin.id == base.id and twin.to_base64() != base.to_base64(), "same permission, fresh random block key"

    assert decide(base, req()) == P.ALLOW
    assert decide(base, req("files.write")) == ("needs_confirmation", "each_write")
    assert decide(base, req(resource=("folder", "/docs/workshop/a"))) == P.deny("resource_not_allowed")
    assert decide(base, req(), now=T0 - 61) == P.deny("not_yet_valid") and decide(base, req(), now=T0 - 60) == P.ALLOW

    narrowed = P.narrow(base, P.Narrowing(actions=("files.read",), resources=(P.Resource("folder", "/docs/work/sub"),)))
    assert narrowed.id == base.id and len(P.revocation_ids(narrowed)) == 2
    assert decide(narrowed, req(resource=("folder", "/docs/work/sub/x"))) == P.ALLOW
    assert decide(narrowed, req()) == P.deny("resource_not_allowed")
    assert decide(narrowed, req("files.write", ("folder", "/docs/work/sub/x"))) == P.deny("action_not_allowed")
    assert P.Mandate.parse(narrowed.to_base64(), pub).levels == narrowed.levels
    with pytest.raises(P.PermissionError) as err:
        P.narrow(narrowed, P.Narrowing(resources=(P.Resource("folder", "/docs/work"),)))
    assert err.value.code == "not_narrower"

    revoked = P.RevocationList()
    revoked.add(P.revocation_ids(narrowed)[1], T0, root)
    assert decide(narrowed, req(resource=("folder", "/docs/work/sub/x")), revoked=revoked) == P.deny("revoked")
    assert decide(base, req(), revoked=revoked) == P.ALLOW, "revoking a narrowing leaves the parent"
    family = P.RevocationList()
    family.add(P.revocation_ids(base)[0], T0, root)
    assert decide(narrowed, req(resource=("folder", "/docs/work/sub/x")), revoked=family) == P.deny("revoked")
    assert P.check_encoded(base.to_base64(), P.public_key_text(P.parse_private_key(OTHER_KEY)), req(), NONE, family, P.SpentSet(), T0) == P.deny("revoked"), \
        "the list is consulted before the signature"


@needs_library
def test_stateful_limits_use_the_callers_figures(base):
    pay = lambda n: req("pay.spend", ("host", "api.example.com"), amounts=(P.Amount("spend", "TWD", n),))  # noqa: E731
    assert decide(base, pay(120), usage={("spend", "TWD", "day"): 1880}) == ("needs_confirmation", "each_write")
    assert decide(base, pay(120), usage={("spend", "TWD", "day"): 1881}) == P.deny("over_limit")
    assert decide(base, pay(120), usage={}) == P.deny("usage_missing"), "a missing figure is never zero"
    assert decide(base, pay(120), usage={("spend", "TWD", "day"): True}) == P.deny("usage_missing")
    assert decide(base, pay(1), usage={("spend", "TWD", "day"): 2**63 - 1}) == P.deny("over_limit")
    assert decide(base, req("pay.spend", ("host", "api.example.com"))) == P.deny("amount_required")
    assert decide(base, req("pay.spend", ("host", "api.example.com"), amounts=(P.Amount("spend", "USD", 1),))) == P.deny("limit_missing")


@needs_library
def test_single_use_approval_and_the_spent_set(base):
    pay = req("pay.spend", ("host", "api.example.com"), amounts=(P.Amount("spend", "TWD", 120),))
    usage = {("spend", "TWD", "day"): 0}
    none = P.RevocationList()
    approval = P.narrow(base, P.Narrowing(actions=("pay.spend",), resources=(P.Resource("host", "api.example.com"),),
                                          caps=(P.Cap("spend", "TWD", 120),), not_after=T0 + 300, card="card-3"))
    assert decide(approval, pay) == P.deny("wrong_card")
    approved = dataclasses.replace(pay, card="card-3")
    spent = P.SpentSet()
    assert P.check_and_record(approval, approved, usage, none, spent, T0 + 10) == P.ALLOW
    assert spent.entries() == [(f"{base.id}/card/card-3", T0 + 360)]
    assert P.check_and_record(approval, approved, usage, none, spent, T0 + 11) == P.deny("already_used")
    assert P.check(approval, approved, usage, none, P.SpentSet(), T0 + 361) == P.deny("expired")
    tiny = P.SpentSet(1)
    tiny.insert("someone/card/else", T0 + 999, T0)
    assert P.check_and_record(approval, approved, usage, none, tiny, T0) == P.deny("spent_set_full"), "a full set refuses instead of forgetting"
    long = P.narrow(base, P.Narrowing(tool="call-1", not_after=T0 + 2 * DAY))
    assert decide(long, req(tool="call-1")) == P.deny("single_use_too_long")
    shorter = P.narrow(long, P.Narrowing(not_after=T0 + 60))
    assert decide(shorter, req(tool="call-1")) == P.deny("single_use_too_long"), "narrowing afterwards never turns a refusal into allow"


# ---------------------------------------------------------------- widening is impossible


def _append_raw(token: str, pub: str, code: str, params: dict | None = None) -> str:
    import biscuit_auth

    biscuit = biscuit_auth.Biscuit.from_base64(token, biscuit_auth.PublicKey(pub))
    block = biscuit_auth.BlockBuilder()
    block.add_code(code, params or {})
    return biscuit.append(block).to_base64()


@needs_library
def test_a_widening_block_changes_nothing(base, pub):
    import biscuit_auth

    narrowed = P.narrow(base, P.Narrowing(actions=("files.read",), resources=(P.Resource("folder", "/docs/work/sub"),)))
    wants = [req("mail.send", ("folder", "/docs/work/sub")), req(resource=("folder", "/etc")),
             req("files.write", ("folder", "/docs/work/sub/a")), req(resource=("folder", "/docs/work/other"))]
    attacks = [
        'action("mail.send"); action("files.write"); resource("folder", "*"); resource("folder", "/etc");',
        "action($a) <- req_action($a); resource($k, $v) <- req_resource($k, $v);",
        "check if true;",
        'holder("helper:vm"); valid_until(9999-01-01T00:00:00Z); privacy_max(3); mandate("m1_x"); schema("muse.permission", 1);',
        'req_action("files.read"); req_resource("folder", "/docs/work/sub");',
    ]
    for attack in attacks:
        token = _append_raw(narrowed.to_base64(), pub, attack)
        for want in wants:
            # 1. our verifier refuses the token outright: the block is not one the schema writes
            assert P.check_encoded(token, pub, want, NONE, P.RevocationList(), P.SpentSet(), T0) == P.deny("unknown_block")
            # 2. and the library alone still refuses, with only the schema's policy and the request as facts
            authorizer = biscuit_auth.AuthorizerBuilder()
            authorizer.add_code('time(2026-11-13T00:00:00Z); time_late(2026-11-13T00:00:00Z); req_holder("helper:vm"); req_privacy(1); '
                                "req_action({a}); req_resource({k}, {v});", {"a": want.action, "k": want.resource.kind, "v": want.resource.value})
            authorizer.add_code(P.AUTHORIZER_POLICY)
            with pytest.raises(biscuit_auth.AuthorizationError):
                authorizer.build(biscuit_auth.Biscuit.from_base64(token, biscuit_auth.PublicKey(pub))).authorize()
    with pytest.raises(Exception):  # "allow if" is not valid in a block at all
        _append_raw(narrowed.to_base64(), pub, "allow if true;")
    # a string holding quotes that prints like a narrowing to two actions: it allows less than it says, never more
    tricky = _append_raw(base.to_base64(), pub, "check if req_action({a});", {"a": 'files.read") or req_action("files.write'})
    assert P.Mandate.parse(tricky, pub).levels[0].actions == ("files.read", "files.write")
    assert P.check_encoded(tricky, pub, req(), NONE, P.RevocationList(), P.SpentSet(), T0) == P.deny("engine_disagreement")
    assert P.check_encoded(tricky, pub, req("pay.spend", ("host", "api.example.com")), NONE, P.RevocationList(), P.SpentSet(), T0) == P.deny("action_not_allowed")


# ---------------------------------------------------------------- properties

ACTIONS = ("files.read", "files.write", "mail.draft", "pay.spend", "llm.call", "llm.paid", "x.custom")
FOLDERS = ("/docs", "/docs/work", "/docs/work/sub", "/docs/workshop", "/文件", "/文件/工作", "/a b")
HOSTS = ("api.example.com", "b.example.com", "c.example.com:8443")
LEVEL = {"deny": 0, "needs_confirmation": 1, "allow": 2}


def _subset(rng: random.Random, items) -> tuple:
    out = tuple(x for x in items if rng.random() < 0.5)
    return out or (rng.choice(items),)


def random_draft(rng: random.Random, issuer: str) -> P.Draft:
    actions = _subset(rng, ACTIONS)
    routine = tuple(a for a in actions if rng.random() < 0.25)
    resources = [P.Resource("folder", f) for f in _subset(rng, FOLDERS)] + [P.Resource("host", h) for h in _subset(rng, HOSTS)]
    resources += rng.choice([[P.Resource("folder", "*")], [P.Resource("host", "*")], [P.Resource("calendar", "work")], [], [], []])
    limits = [P.Limit("spend", "TWD", 2, "day", 100 + rng.randrange(2000)), P.Limit("requests", "", 0, "day", 1 + rng.randrange(50))]
    if rng.random() < 0.5:
        limits.append(P.Limit("spend", "TWD", 2, "month", 3000 + rng.randrange(5000)))
    start = T0 + rng.randrange(1000)
    quiet = all(P.action_class(a) == "read" or a in routine for a in actions)
    confirm = rng.choice([P.EACH_WRITE, P.Confirm("over_amount", "TWD", 2, rng.randrange(600)), P.NEVER if quiet else P.EACH_WRITE])
    return P.propose_from_fields(base_draft(issuer, actions=actions, routine=routine, resources=tuple(set(resources)), limits=tuple(limits),
                                            not_before=start, not_after=start + 1 + rng.randrange(3 * DAY), privacy=rng.choice(P.PRIVACY), confirm=confirm))


def random_narrowing(rng: random.Random, m: P.Mandate) -> P.Narrowing | None:
    f: dict = {}
    if rng.random() < 0.5 and m.actions():
        f["actions"] = _subset(rng, m.actions())
    if rng.random() < 0.5:
        pool = [P.Resource("folder", x) for x in FOLDERS + ("/docs/work/sub/deep", "*")] + [P.Resource("host", x) for x in HOSTS + ("*",)] + [P.Resource("calendar", "work")]
        pool = [c for c in pool if any(e.covers(c) for e in m.resources())]
        if pool:
            f["resources"] = _subset(rng, pool)[:4]
    if rng.random() < 0.33:
        l = rng.choice(m.limits())
        f["limits"] = (P.NarrowLimit(l.kind, l.unit, l.period, rng.randrange(l.amount + 1)),)
    if rng.random() < 0.33:
        old = next((c.amount for c in m.caps() if c.kind == "spend"), 5000)
        f["caps"] = (P.Cap("spend", "TWD", rng.randrange(old + 1)),)
    start, end = m.not_before(), m.not_after()
    if rng.random() < 0.33:
        f["not_before"] = start + rng.randrange(end - start + 1)
    if rng.random() < 0.33:
        first = f.get("not_before", start)
        f["not_after"] = first + rng.randrange(end - first + 1)
    if rng.random() < 0.33:
        f["privacy"] = P.PRIVACY[rng.randrange(P.PRIVACY.index(m.privacy()) + 1)]
    pick = rng.randrange(6)
    if pick < 2:
        f["confirm"] = P.EACH_WRITE if pick == 0 else P.Confirm("over_amount", "TWD", 2, rng.randrange(600))
    if rng.random() < 0.25:
        f["job"] = (m.jobs() or (f"job-{rng.randrange(3)}",))[0]
    for name, bound, chance in (("tool", m.tools(), 0.125), ("card", m.cards(), 0.125)):
        if rng.random() < chance:
            f[name] = (bound or (f"{name}-{rng.randrange(3)}",))[0]
            f.setdefault("not_after", end)
    return P.Narrowing(**f) if f else None


def boundary_requests(rng: random.Random, m: P.Mandate):
    """Requests around every boundary: path segments, amounts at each limit, cap and
    threshold plus and minus one, times at each end plus and minus the tolerance, each
    privacy class, a wrong holder, and right, wrong and missing job, tool and card ids."""
    folders = list(FOLDERS)
    for r in list(m.resources()) + list(m.draft.resources):
        if r.kind == "folder" and r.value != "*":
            folders += [r.value, r.value + "/x", r.value + "x", r.value + "/x/y"]
            if r.value.rfind("/") > 0:
                folders.append(r.value[: r.value.rfind("/")])
    resources = [("folder", f) for f in folders] + [("host", h) for h in HOSTS] + [("host", "evil.example.com"), ("calendar", "work"), ("calendar", "other")]
    times = [T0 + 500]
    for edge in (m.not_before(), m.not_after(), m.draft.not_before, m.draft.not_after):
        times += [edge - 61, edge - 60, edge - 1, edge, edge + 1, edge + 60, edge + 61]
    amounts = [0, 1]
    for l in list(m.limits()) + list(m.draft.limits):
        if l.kind == "spend":
            amounts += [l.amount - 1, l.amount, l.amount + 1, l.amount // 2]
    for c in m.caps():
        amounts += [c.amount - 1, c.amount, c.amount + 1]
    for rule in m.confirm_rules():
        if rule.rule == "over_amount":
            amounts += [rule.amount - 1, rule.amount, rule.amount + 1]
    amounts = [a for a in amounts if a >= 0]
    ids = {name: [None, f"{name}-0", f"{name}-1", f"{name}-9"] + list(bound) for name, bound in (("job", m.jobs()), ("tool", m.tools()), ("card", m.cards()))}
    for _ in range(40):
        aim = rng.random() < 0.75  # mostly aim inside, so the boundaries are hit from both sides
        action = rng.choice(m.actions()) if aim and m.actions() else rng.choice(ACTIONS)
        privacy = P.PRIVACY[rng.randrange(P.PRIVACY.index(m.privacy()) + 1)] if aim else rng.choice(P.PRIVACY)
        amount = []
        if P.action_class(action) == "spend" or rng.random() < 0.2:
            amount.append(P.Amount("spend", "TWD", rng.choice(amounts)))
        if rng.random() < 0.25:
            amount.append(P.Amount("requests", "", 1))
        bound = {name: (values[-1] if aim and values[-1] is not None and len(values) > 4 else rng.choice(values)) for name, values in ids.items()}
        request = req(action, rng.choice(resources), holder="helper:other" if rng.random() < 0.08 else "helper:vm", privacy=privacy,
                      amounts=tuple(amount), job=bound["job"], tool=bound["tool"], card=bound["card"])
        usage = {}
        for l in m.limits():
            carried = next((a.value for a in amount if a.kind == l.kind), 0)
            used = rng.choice([0, l.amount, rng.randrange(l.amount + 1), max(l.amount - carried, 0)])
            if rng.random() > 0.07:
                usage[(l.kind, l.unit, l.period)] = used
        yield request, usage, rng.choice(times)


@needs_library
def test_a_narrowed_token_never_does_more_than_its_parent(root, pub):
    none, spent = P.RevocationList(), P.SpentSet()
    seen = {"allow": 0, "needs_confirmation": 0, "deny": 0, "narrowed allow": 0, "narrowings": 0}
    for seed in range(1, 41):
        rng = random.Random(seed)
        chain = [P.issue(random_draft(rng, pub), root)]
        for _ in range(1 + rng.randrange(4)):
            n = random_narrowing(rng, chain[-1])
            if n is not None:
                child = P.narrow(chain[-1], n)
                assert P.Mandate.parse(child.to_base64(), pub).levels == child.levels, f"seed {seed}"
                chain.append(child)
                seen["narrowings"] += 1
        last = chain[-1]
        for request, usage, now in boundary_requests(rng, last):
            decisions = [P.check(m, request, usage, none, spent, now) for m in chain]
            for d in decisions:
                assert d != P.deny("engine_disagreement"), f"seed {seed}: {request}"
                seen[d.kind] += 1
            seen["narrowed allow"] += len(chain) > 1 and decisions[-1].allowed
            for i in range(len(chain) - 1):
                parent, child = decisions[i], decisions[i + 1]
                # A card binding is the person's approval: the one thing that turns "ask" into
                # "allow". It never turns a refusal into anything.
                approved_here = not chain[i].cards() and chain[i + 1].cards()
                ok = (LEVEL[child.kind] == 0 or LEVEL[parent.kind] >= 1) if approved_here else LEVEL[child.kind] <= LEVEL[parent.kind]
                assert ok, f"seed {seed}, level {i}: parent {parent}, child {child}\n{request}\n{usage} now {now}\nchild adds {chain[i + 1].levels[-1]}"
            # the sentence's intersection agrees with checking every level
            listed = request.action in last.actions() and any(e.covers(request.resource) for e in last.resources())
            per_level = (request.action in last.draft.actions and all(not l.actions or request.action in l.actions for l in last.levels)
                         and any(e.covers(request.resource) for e in last.draft.resources)
                         and all(not l.resources or any(e.covers(request.resource) for e in l.resources) for l in last.levels))
            assert listed == per_level, f"seed {seed}: describe would differ from check for {request}"
    # the generator must really exercise both sides of the boundaries
    assert seen["narrowings"] > 60 and seen["allow"] > 100 and seen["needs_confirmation"] > 30 and seen["narrowed allow"] > 15, seen


@needs_library
def test_the_sentence_says_everything_that_widens(root, pub):
    for seed in range(1, 31):
        m = P.issue(random_draft(random.Random(1000 + seed), pub), root)
        for lang in P.LANGS:
            text = P.describe(m, lang)
            everything = "\n".join((text.sentence,) + text.lines)
            assert "vm" in text.sentence
            for action in m.draft.actions:
                alone = P.describe(P.issue(dataclasses.replace(m.draft, actions=(action,), routine=(), confirm=P.EACH_WRITE), root), lang).sentence
                phrase = re.search(r" may (.*), limited to|可以(.*)，範圍", alone)
                assert (phrase.group(1) or phrase.group(2)) in text.sentence, f"seed {seed}: {action} missing"
            for r in m.draft.resources:
                assert (("any " if lang == "en" else "任何") if r.value == "*" else r.value) in text.sentence, f"seed {seed}: {r}"
            for l in m.draft.limits:
                assert any(P.format_amount(l.kind, l.unit, l.decimals, l.amount, lang) in line for line in text.lines), f"seed {seed}: {l}"
            assert P.format_time(m.draft.not_before) in everything and P.format_time(m.draft.not_after) in everything
            assert "Asia/Taipei" in everything
            if m.draft.routine and m.draft.confirm != P.NEVER:
                assert any(line.startswith(("Without asking", "不必每次詢問")) for line in text.lines)
            if m.draft.confirm.rule == "over_amount":
                assert P.format_amount("spend", "TWD", 2, m.draft.confirm.amount, lang) in everything
            if m.draft.confirm == P.NEVER:
                assert "Does not ask" in everything or "不會先問" in everything
            classes = {"\n".join(P.describe(P.issue(dataclasses.replace(m.draft, privacy=p), root), lang).lines) for p in P.PRIVACY}
            assert len(classes) == 4, "each privacy class has its own line"
    with pytest.raises(P.PermissionError):
        P.describe(m, "fr")


# ---------------------------------------------------------------- revocation list


@needs_library
def test_revocation_list_is_signed_and_merges_by_union(root, pub, base):
    ident, other = P.revocation_ids(base)[0], "ab" * 64
    a, b = P.RevocationList(), P.RevocationList()
    a.add(ident, T0 + 50, root)
    b.add(ident, T0 + 20, root)
    b.add(other, T0 + 30, root)
    ab, ba = P.RevocationList(), P.RevocationList()
    for target, order in ((ab, (a, b)), (ba, (b, a))):
        for part in order:
            target.merge(part)
    assert ab == ba and len(ab) == 2, "commutative"
    assert {e.id: e.at for e in ab.entries()} == {ident: T0 + 20, other: T0 + 30}, "the earlier time wins"
    ab.merge(a)
    ab.merge(ab)
    assert ab == ba, "idempotent"
    ab.verify(pub)
    assert P.RevocationList.from_json(ab.to_json(), pub) == ab
    stranger = P.parse_private_key(OTHER_KEY)
    forged = P.RevocationEntry.sign(ident, T0, stranger)
    for bad in (forged.token, base.to_base64(), "not a token", 7):
        with pytest.raises(P.PermissionError) as err:
            P.RevocationList().add_entry(bad, pub)
        assert err.value.code == "invalid_revocation"
    for text in ("{", "[]", '{"v": 2, "entries": []}', '{"v": 1.0, "entries": []}', '{"v": 1, "entries": [1]}', json.dumps({"v": 1, "entries": [forged.token]})):
        with pytest.raises(P.PermissionError):
            P.RevocationList.from_json(text, pub)
    with pytest.raises(P.PermissionError):
        P.RevocationList().add("short", T0, root)


# ---------------------------------------------------------------- the home's files


@needs_library
def test_key_file_and_token_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MUSE_HOME", str(tmp_path))
    source = store.FileKeySource()
    assert not source.exists()
    with pytest.raises(P.PermissionError) as err:
        source.load()
    assert err.value.code == "no_root_key"
    pub = source.create()
    assert pub.startswith("ed25519/") and source.public() == pub
    with pytest.raises(P.PermissionError) as err:
        source.create()
    assert err.value.code == "key_exists", "a root key is never replaced silently"
    if os.name == "posix":
        assert stat.S_IMODE(source.path.stat().st_mode) == 0o600
        source.path.chmod(0o644)
        with pytest.raises(P.PermissionError) as err:
            source.load()
        assert err.value.code == "insecure_key_file"
        source.path.chmod(0o600)
    root = source.load()
    tokens = store.PermissionStore()
    assert tokens.names() == []
    m = P.issue(base_draft(pub), root)
    name = tokens.save(m)
    child = P.narrow(m, P.Narrowing(job="job-1"))
    child_name = tokens.save(child)
    assert name == m.id[3:15] and child_name.startswith(name + "-") and tokens.names() == [name, child_name]
    assert tokens.save(m) == name, "saving the same token again is the same file"
    assert tokens.save(P.issue(base_draft(pub), root)) not in (name, child_name), "the same draft signed again is another token"
    assert tokens.resolve(name) == name and tokens.resolve(child_name[:15]) == child_name
    with pytest.raises(P.PermissionError) as err:
        tokens.resolve("zz")
    assert err.value.code == "not_found"
    assert tokens.load(child_name, pub).levels == child.levels
    if os.name == "posix":
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in tokens.dir.iterdir())
        assert stat.S_IMODE(tokens.dir.stat().st_mode) == 0o700
    revoked = tokens.revocations(pub)
    revoked.add(P.revocation_ids(m)[0], T0, root)
    tokens.save_revocations(revoked)
    assert tokens.revocations(pub) == revoked
    spent = tokens.spent()
    spent.insert("k", T0 + 5, T0)
    tokens.save_spent(spent)
    assert tokens.spent().entries() == [("k", T0 + 5)]
    (tokens.dir / "spent.json").write_text("{", encoding="utf-8")
    with pytest.raises(P.PermissionError) as err:
        tokens.spent()
    assert err.value.code == "invalid_spent_set", "an unreadable spent set must not read as empty"


# ---------------------------------------------------------------- command line


@pytest.fixture()
def muse(tmp_path, monkeypatch, capsys):
    for name in list(keys.KNOWN_KEYS):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MUSE_ENV_FILE", str(tmp_path / ".env"))
    monkeypatch.setenv("MUSE_HOME", str(tmp_path / "home"))
    monkeypatch.setitem(sys.modules, "keyring", None)

    def run(*argv: str) -> tuple[int, str, str]:
        code = cli.main(["permissions", *argv])
        captured = capsys.readouterr()
        return code, captured.out, captured.err

    return run


ISSUE = ("issue", "--holder", "helper:vm", "--action", "files.read", "--action", "pay.spend", "--resource", "folder:/Documents/Work",
         "--resource", "host:api.example.com", "--limit", "spend:TWD:2:day:2000", "--purpose", "Weekly digest", "--zone", "Asia/Taipei")


@needs_library
def test_cli_flow(muse, tmp_path):
    assert muse("list")[1].startswith("No permissions yet")
    code, _, err = muse(*ISSUE, "--yes")
    assert code == 1 and "--new-key" in err, "a root key is only created when asked for"
    code, out, err = muse(*ISSUE, "--new-key")
    assert code == 1 and "confirmation" in err and "may read files and spend money" in out, "shows the sentence, then needs a yes"
    assert not list((tmp_path / "home" / "permissions").glob("*.token")), "nothing is stored without the yes"
    code, out, _ = muse(*ISSUE, "--yes", "--lang", "zh-TW", "--at", "2026-11-13")
    assert code == 0 and "你的助手「vm」可以讀取檔案、付款" in out and "每日最多 NT$20" in out
    name = re.search(r"Issued (\S+)", out).group(1)

    code, out, _ = muse("list", "--json")
    rows = json.loads(out)
    assert [r["name"] for r in rows] == [name] and rows[0]["state"] in ("active", "not yet valid", "expired")
    at = ("--at", "2026-11-13T00:00:05Z")
    assert "[active]" in muse("show", name[:6], *at)[1]
    assert muse("check", name, "--action", "files.read", "--resource", "folder:/Documents/Work/a.md", *at)[:2] == (0, "allow\n")
    assert muse("check", name, "--action", "files.read", "--resource", "folder:/Documents/Workshop/a.md", *at)[:2] == (3, "deny: resource_not_allowed\n")
    pay = ("--action", "pay.spend", "--resource", "host:api.example.com", "--amount", "spend:TWD:120", "--used", "spend:TWD:day:0")
    assert muse("check", name, *pay, *at)[:2] == (3, "needs confirmation: each_write\n")
    assert muse("check", name, "--action", "pay.spend", "--resource", "host:api.example.com", "--amount", "spend:TWD:120", "--used", "spend:TWD:day:1881", *at)[1] == "deny: over_limit\n"

    code, out, _ = muse("narrow", name, "--action", "pay.spend", "--cap", "spend:TWD:120", "--until", "2026-11-13T00:05:00Z", "--card", "card-3", *at)
    assert code == 0 and "Only for one approval (card-3)" in out and "no key was needed" in out
    card = re.search(r"Saved (\S+)", out).group(1)
    assert muse("check", card, *pay, "--card", "card-3", "--record", *at)[:2] == (0, "allow\n")
    assert muse("check", card, *pay, "--card", "card-3", "--record", *at)[:2] == (3, "deny: already_used\n")
    code, _, err = muse("narrow", name, "--action", "mail.send")
    assert code == 1 and "not_narrower" in err
    assert muse("narrow", name, "--resource", "nonsense")[0] == 2
    assert muse("narrow", name, "--until", "tomorrow")[0] == 2

    assert muse("revoke", name)[0] == 1, "stopping needs a yes too"
    code, out, _ = muse("revoke", name, "--yes", *at)
    assert code == 0 and "Stopped" in out
    assert muse("check", name, "--action", "files.read", "--resource", "folder:/Documents/Work/a.md", *at)[:2] == (3, "deny: revoked\n")
    assert muse("check", card, *pay, "--card", "card-3", *at)[:2] == (3, "deny: revoked\n"), "and every copy narrowed from it"
    assert muse("list", *at)[1].count("[stopped]") == 2
    assert muse("show", "nothing")[0] == 1
    if os.name == "posix":
        home = tmp_path / "home"
        assert stat.S_IMODE((home / "root.key").stat().st_mode) == 0o600
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in (home / "permissions").iterdir())
