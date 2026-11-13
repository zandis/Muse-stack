"""The hand-written OpenAPI file is kept honest: it lists exactly the routes the server serves."""

from __future__ import annotations

import re

import pytest
import yaml

from home_fakes import CARD_SCHEMA_PATH, OPENAPI_PATH, card_schema
from muse.home import API_VERSION, api, cards, server

METHODS = ("get", "post", "put", "patch", "delete", "head", "options", "trace")
APP_STATUS_FIELDS = {"money", "activity", "local_runtime", "restart_pending"}  # added to the snapshot by muse/home/app_api.py


@pytest.fixture(scope="module")
def spec():
    return yaml.safe_load(OPENAPI_PATH.read_text(encoding="utf-8"))


def documented(spec) -> set[tuple[str, str]]:
    return {(method.upper(), path) for path, item in spec["paths"].items() for method in item if method in METHODS}


def test_every_route_is_in_the_file_and_every_listed_route_is_served(spec):
    served = set(api.route_table())
    listed = documented(spec)
    assert served - listed == set(), "served but missing from home-api.v1.yaml"
    assert listed - served == set(), "in home-api.v1.yaml but not served"
    assert len(api.route_table()) == len(served) == 53  # no route is declared twice


def test_a_route_added_to_the_table_would_fail_this_test(spec, monkeypatch):
    extra = api.Route("GET", "/v1/surprise", api.h_not_built)
    monkeypatch.setattr(api, "ROUTES", api.ROUTES + (extra,))
    assert set(api.route_table()) - documented(spec) == {("GET", "/v1/surprise")}
    monkeypatch.setattr(api, "ROUTES", tuple(r for r in api.ROUTES if r.path not in ("/v1/surprise", "/v1/mandates/artifacts")))
    assert documented(spec) - set(api.route_table()) == {("GET", "/v1/mandates/artifacts")}


def test_the_table_is_exactly_the_architecture_list_plus_today(spec):
    assert sorted(api.route_table()) == sorted([
        ("GET", "/v1/status"), ("GET", "/v1/catalogue"), ("POST", "/v1/connect/{provider}"), ("POST", "/v1/keys/{provider}"),
        ("GET", "/v1/jobs"), ("POST", "/v1/jobs"), ("GET", "/v1/jobs/{id}"), ("DELETE", "/v1/jobs/{id}"),
        ("GET", "/v1/cards"), ("POST", "/v1/cards"), ("GET", "/v1/cards/{id}"), ("POST", "/v1/cards/{id}/answer"),
        ("POST", "/v1/pause"), ("POST", "/v1/resume"),
        ("GET", "/v1/apps"), ("POST", "/v1/apps"), ("PATCH", "/v1/apps/{name}"), ("DELETE", "/v1/apps/{name}"),
        ("POST", "/v1/apps/{name}/rotate"), ("GET", "/v1/receipts"), ("POST", "/v1/receipts"),
        ("GET", "/v1/skills"), ("POST", "/v1/skills/{name}/run"), ("GET", "/v1/skills/runs/{id}"),
        ("GET", "/v1/mandates/artifacts"),
        # The app's screens (muse/home/app_api.py).
        ("POST", "/v1/ask"), ("GET", "/v1/ask/{id}"), ("POST", "/v1/ask/{id}/choose"),
        ("GET", "/v1/conversations"), ("GET", "/v1/conversations/{id}"), ("DELETE", "/v1/conversations/{id}"),
        ("GET", "/v1/chores"), ("POST", "/v1/chores"), ("POST", "/v1/chores/understand"), ("PATCH", "/v1/chores/{id}"),
        ("DELETE", "/v1/chores/{id}"), ("POST", "/v1/chores/{id}/run"), ("GET", "/v1/chores/{id}/history"),
        ("GET", "/v1/services"), ("POST", "/v1/services/{id}/test"), ("DELETE", "/v1/services/{id}"),
        ("POST", "/v1/connect/{provider}/start"), ("GET", "/v1/connect/flows/{state_id}"),
        ("POST", "/v1/connect/flows/{state_id}/code"), ("DELETE", "/v1/connect/flows/{state_id}"),
        ("POST", "/v1/home/restart-proxy"), ("GET", "/v1/apps/presets"), ("GET", "/v1/settings"), ("PATCH", "/v1/settings"),
        ("GET", "/v1/memory"), ("DELETE", "/v1/memory"), ("POST", "/v1/verify"), ("GET", "/v1/about"),
    ])
    assert all(path.startswith(f"/v{API_VERSION}/") for _, path in api.route_table())
    assert spec["info"]["version"] == API_VERSION and spec["openapi"].startswith("3.1")


def test_planned_routes_are_marked_planned_in_both_places(spec):
    planned = {(r.method, r.path) for r in api.ROUTES if r.planned}
    assert planned == {("GET", "/v1/mandates/artifacts")}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            is_planned = (method.upper(), path) in planned
            assert bool(op.get("x-planned")) == is_planned, (method, path)
            assert ("501" in op["responses"]) == is_planned, (method, path)


def test_only_the_keys_route_may_take_a_key():
    # A provider's key, and the one-time code of a one-tap connection (it is traded for the key at once).
    assert [(r.method, r.path) for r in api.ROUTES if r.takes_key] == [
        ("POST", "/v1/keys/{provider}"), ("POST", "/v1/connect/flows/{state_id}/code")]


def test_path_parameters_are_declared(spec):
    for path, item in spec["paths"].items():
        names = set(re.findall(r"\{(\w+)\}", path))
        for method, op in item.items():
            declared = set()
            for p in op.get("parameters", []):
                if "$ref" in p:
                    p = spec["components"]["parameters"][p["$ref"].rsplit("/", 1)[1]]
                if p["in"] == "path":
                    assert p["required"] is True
                    declared.add(p["name"])
            assert declared == names, (method, path)


def test_bodies_are_documented_where_the_server_takes_one(spec):
    takes_body = {("POST", "/v1/keys/{provider}"), ("POST", "/v1/jobs"), ("POST", "/v1/cards"), ("POST", "/v1/cards/{id}/answer"), ("POST", "/v1/apps"),
                  ("PATCH", "/v1/apps/{name}"), ("POST", "/v1/receipts"), ("POST", "/v1/skills/{name}/run"),
                  ("POST", "/v1/ask"), ("POST", "/v1/ask/{id}/choose"), ("POST", "/v1/chores"), ("POST", "/v1/chores/understand"),
                  ("PATCH", "/v1/chores/{id}"), ("POST", "/v1/connect/{provider}/start"),
                  ("POST", "/v1/connect/flows/{state_id}/code"), ("PATCH", "/v1/settings")}
    for path, item in spec["paths"].items():
        for method, op in item.items():
            assert ("requestBody" in op) == ((method.upper(), path) in takes_body), (method, path)


def test_every_operation_has_an_id_a_summary_and_the_error_shape(spec):
    ids = []
    for path, item in spec["paths"].items():
        for method, op in item.items():
            ids.append(op["operationId"])
            assert op["summary"], (method, path)
            assert op["responses"]["default"] == {"$ref": "#/components/responses/Error"}, (method, path)
    assert len(ids) == len(set(ids))
    for name, response in spec["components"]["responses"].items():
        assert response["content"]["application/json"]["schema"] == {"$ref": "#/components/schemas/Error"}, name


def test_error_codes_match_the_server(spec):
    assert spec["x-error-codes"] == api.ERROR_CODES
    error = spec["components"]["schemas"]["Error"]["properties"]["error"]
    assert set(error["properties"]["code"]["enum"]) == set(api.ERROR_CODES)
    assert set(error["properties"]) == {"code", "message", "retry_after"} and error["required"] == ["code", "message"]
    for code, status in api.ERROR_CODES.items():
        assert f"`{code}` {status}" in spec["info"]["description"], code


def test_security_is_a_bearer_device_token_on_every_call(spec):
    assert spec["security"] == [{"deviceToken": []}]
    assert spec["components"]["securitySchemes"]["deviceToken"]["scheme"] == "bearer"
    assert not any("security" in op for item in spec["paths"].values() for op in item.values())  # no call opts out


def test_stated_limits_match_the_code(spec):
    text = spec["info"]["description"]
    assert "512 KiB" in text and api.MAX_BODY_BYTES == 512 * 1024
    assert f"127.0.0.1:{server.DEFAULT_TCP_PORT}" in spec["servers"][0]["url"]
    job = spec["components"]["schemas"]["NewJob"]["properties"]
    from muse import queue

    assert job["prompt"]["maxLength"] == queue.MAX_PROMPT_CHARS and job["prompts"]["maxItems"] == queue.MAX_BATCH_ITEMS
    assert job["max_tokens"]["default"] == queue.DEFAULT_MAX_TOKENS and job["max_attempts"]["default"] == queue.DEFAULT_MAX_ATTEMPTS
    assert set(spec["components"]["schemas"]["Job"]["properties"]["status"]["enum"]) == set(queue.STATUSES)
    assert set(spec["components"]["schemas"]["Job"]["properties"]) == set(queue.PUBLIC_COLUMNS.replace(" ", "").split(","))


def test_the_card_schema_is_the_one_file(spec):
    assert spec["components"]["schemas"]["Card"] == {"$ref": "card.schema.json"}
    assert (OPENAPI_PATH.parent / "card.schema.json") == CARD_SCHEMA_PATH and CARD_SCHEMA_PATH.exists()
    schema = card_schema()
    props = schema["properties"]
    assert props["title"]["maxLength"] == cards.MAX_TITLE and props["thing"]["maxLength"] == cards.MAX_THING
    assert props["where"]["maxLength"] == cards.MAX_WHERE and props["under"]["properties"]["text"]["maxLength"] == cards.MAX_UNDER
    assert schema["$defs"]["money"]["properties"]["amount"]["pattern"] == cards.AMOUNT_RE.pattern
    assert schema["$defs"]["money"]["properties"]["currency"]["pattern"] == cards.CURRENCY_RE.pattern
    assert schema["$defs"]["ref"]["pattern"] == cards.REF_RE.pattern and props["id"]["pattern"] == cards.ID_RE.pattern
    kinds = [rule["if"]["properties"]["kind"]["const"] for rule in schema["allOf"] if "kind" in rule["if"]["properties"]]
    assert sorted(set(kinds)) == sorted(cards.KINDS)
    new = spec["components"]["schemas"]["NewCard"]["properties"]
    assert sorted(new["kind"]["enum"]) == sorted(cards.KINDS) and new["ttl_s"]["maximum"] == cards.MAX_TTL_S
    assert (new["title"]["maxLength"], new["thing"]["maxLength"], new["where"]["maxLength"], new["under"]["maxLength"]) == (
        cards.MAX_TITLE, cards.MAX_THING, cards.MAX_WHERE, cards.MAX_UNDER)
    assert f"at most {api.MAX_PENDING_PER_DEVICE} cards" in spec["paths"]["/v1/cards"]["post"]["description"]


def test_app_and_status_shapes_match_what_is_served(spec):
    from muse.types import Snapshot

    status = spec["components"]["schemas"]["Status"]
    assert set(status["properties"]) == set(Snapshot.__annotations__) | {"api_version"} | APP_STATUS_FIELDS
    assert set(status["required"]) == set(status["properties"]) - {"home"}
    app = spec["components"]["schemas"]["App"]
    assert set(app["required"]) == set(app["properties"]) == {"name", "priority", "daily_requests", "daily_tokens", "allow_paid",
                                                               "default_privacy", "note", "created_at"}
    assert set(spec["components"]["schemas"]["AppSettings"]["properties"]) == set(api._APP_FIELDS)


def test_every_operation_states_its_scope_and_roles_and_they_match_the_server(spec):
    from muse.home import auth

    for route in api.ROUTES:
        op = spec["paths"][route.path][route.method.lower()]
        assert op["x-scope"] == route.scope, (route.method, route.path)
        assert op["x-roles"] == api.route_roles(route), (route.method, route.path)
        assert op["x-roles"] and op["x-roles"][0] == "owner"  # the person can always do it
    assert {role: sorted(scopes) for role, scopes in spec["x-role-scopes"].items()} == {
        role: sorted(scopes) for role, scopes in auth.ROLE_SCOPES.items()}
    text = spec["info"]["description"]
    assert "`forbidden` (403)" in text and "created before roles existed are `owner`" in text
    assert f"more than {server.AUTH_FAIL_LIMIT} times in {int(server.AUTH_FAIL_WINDOW_S)} seconds" in text
    assert f"At most {server.MAX_CONNECTIONS} connections" in text
