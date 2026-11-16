"""Shared helpers for the home control API tests: a running server, a client, and a small JSON Schema checker."""

from __future__ import annotations

import json
import re
import shutil
import socket
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from wardmuse.home import api, server
from wardmuse.home.auth import StaticVerifier
from wardmuse.home.client import HomeClient

ROOT = Path(__file__).resolve().parents[1]
CARD_SCHEMA_PATH = ROOT / "docs" / "spec" / "interfaces" / "card.schema.json"
OPENAPI_PATH = ROOT / "docs" / "spec" / "interfaces" / "home-api.v1.yaml"

DEVICE = "test-phone"
TOKEN = "device-token-for-tests"  # plain words: the real ones are random


def verifier() -> StaticVerifier:
    return StaticVerifier({DEVICE: TOKEN})


@contextmanager
def short_dir() -> Iterator[Path]:
    """A short private folder: a Unix socket path must fit in about 100 bytes."""
    path = Path(tempfile.mkdtemp(prefix="mh-"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@contextmanager
def running(home: api.Home, auth: Any = None, *, tcp: bool = False, socket_path: Path | None = None) -> Iterator[server.HomeServer]:
    """A server on a thread; Unix socket by default, 127.0.0.1 on a free port with tcp=True."""
    with short_dir() as folder:
        if tcp:
            srv = server.make_server(home, auth or verifier(), tcp_port=0)
        else:
            srv = server.make_server(home, auth or verifier(), socket_path=socket_path or folder / "home.sock")
        thread = server.serve_in_thread(srv)
        try:
            yield srv
        finally:
            srv.shutdown()
            srv.server_close()
            thread.join(timeout=5)


def client_for(srv: server.HomeServer, token: str | None = TOKEN) -> HomeClient:
    if srv.is_tcp:
        return HomeClient(token, tcp_port=srv.port, timeout=10)
    return HomeClient(token, socket_path=srv.socket_path, timeout=10)


def raw_exchange(srv: server.HomeServer, data: bytes, timeout: float = 5.0) -> bytes:
    """Send raw bytes and return everything the server sends back before it closes."""
    if srv.is_tcp:
        sock = socket.create_connection(("127.0.0.1", srv.port), timeout=timeout)
    else:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(srv.socket_path))
    try:
        sock.sendall(data)
        chunks = []
        while True:
            try:
                chunk = sock.recv(65536)
            except (TimeoutError, ConnectionError):
                break
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        sock.close()


def split_response(raw: bytes) -> tuple[int, dict[str, str], bytes]:
    head, _, body = raw.partition(b"\r\n\r\n")
    lines = head.decode("latin-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {k.strip().lower(): v.strip() for k, v in (line.split(":", 1) for line in lines[1:] if ":" in line)}
    return status, headers, body


def assert_error(status: int, payload: Any, want_status: int, code: str) -> dict[str, Any]:
    """Every error has one shape, and its code maps to its status."""
    assert status == want_status, (status, payload)
    assert isinstance(payload, dict) and set(payload) == {"error"}, payload
    err = payload["error"]
    assert set(err) <= {"code", "message", "retry_after"} and err["code"] == code, err
    assert api.ERROR_CODES[code] == want_status
    assert isinstance(err["message"], str) and err["message"] and "\n" not in err["message"]
    assert "Traceback" not in err["message"] and "Error(" not in err["message"]
    return err


# ---------------------------------------------------------------- JSON Schema (the subset the card schema uses)


def card_schema() -> dict[str, Any]:
    return json.loads(CARD_SCHEMA_PATH.read_text(encoding="utf-8"))


_TYPES = {
    "object": dict, "array": list, "string": str, "boolean": bool, "null": type(None),
}


def _is_type(value: Any, name: str) -> bool:
    if name == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if name == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, _TYPES[name])


def schema_errors(value: Any, schema: dict[str, Any], root: dict[str, Any] | None = None, path: str = "$") -> list[str]:
    """Problems found validating `value` against `schema`. Empty means valid."""
    root = schema if root is None else root
    known = {"$schema", "$id", "title", "description", "$ref", "type", "enum", "const", "required", "properties",
             "additionalProperties", "minLength", "maxLength", "pattern", "minimum", "items", "minItems", "maxItems",
             "oneOf", "anyOf", "allOf", "if", "then", "$defs"}
    assert set(schema) <= known, f"the test validator does not know {set(schema) - known}"
    out: list[str] = []
    if "$ref" in schema:
        ref = schema["$ref"]
        assert ref.startswith("#/"), ref
        target: Any = root
        for part in ref[2:].split("/"):
            target = target[part]
        out += schema_errors(value, target, root, path)
    if "type" in schema:
        names = schema["type"] if isinstance(schema["type"], list) else [schema["type"]]
        if not any(_is_type(value, n) for n in names):
            return out + [f"{path}: not of type {names}"]
    if "enum" in schema and value not in schema["enum"]:
        out.append(f"{path}: not one of {schema['enum']}")
    if "const" in schema and not (value == schema["const"] and type(value) is type(schema["const"])):
        out.append(f"{path}: not {schema['const']!r}")
    if isinstance(value, str):
        if len(value) < schema.get("minLength", 0):
            out.append(f"{path}: too short")
        if len(value) > schema.get("maxLength", 10**9):
            out.append(f"{path}: too long")
        if "pattern" in schema and not re.search(schema["pattern"], value):
            out.append(f"{path}: does not match {schema['pattern']}")
    if _is_type(value, "number") and "minimum" in schema and value < schema["minimum"]:
        out.append(f"{path}: below minimum")
    if isinstance(value, list):
        if len(value) < schema.get("minItems", 0) or len(value) > schema.get("maxItems", 10**9):
            out.append(f"{path}: wrong number of items")
        if "items" in schema:
            for i, item in enumerate(value):
                out += schema_errors(item, schema["items"], root, f"{path}[{i}]")
    if isinstance(value, dict):
        for name in schema.get("required", []):
            if name not in value:
                out.append(f"{path}: missing {name}")
        props = schema.get("properties", {})
        for name, sub in props.items():
            if name in value:
                out += schema_errors(value[name], sub, root, f"{path}.{name}")
        if schema.get("additionalProperties") is False:
            for name in value:
                if name not in props:
                    out.append(f"{path}: unexpected {name}")
    for sub in schema.get("allOf", []):
        out += schema_errors(value, sub, root, path)
    if "anyOf" in schema and not any(not schema_errors(value, s, root, path) for s in schema["anyOf"]):
        out.append(f"{path}: matches none of anyOf")
    if "oneOf" in schema and sum(1 for s in schema["oneOf"] if not schema_errors(value, s, root, path)) != 1:
        out.append(f"{path}: does not match exactly one of oneOf")
    if "if" in schema and not schema_errors(value, schema["if"], root, path) and "then" in schema:
        out += schema_errors(value, schema["then"], root, path)
    return out
