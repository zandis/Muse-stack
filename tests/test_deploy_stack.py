"""The always-on VM stack (work package D4): deploy/oracle-stack and deploy/vm.

No cloud and no network. Nothing here talks to Oracle; a real tenancy is the
only real test (deploy/oracle-stack/README.md, "Not tested yet").

Tools: `terraform` is not installed where this suite was written, so the .tf
files are checked with the small strict HCL reader below (blocks, attributes,
balanced brackets and strings, duplicate attributes, every reference declared).
It is a sanity check of the subset this stack uses, not `terraform validate`.
`shellcheck` runs when it is installed and is skipped otherwise; every script
always goes through `bash -n`. The encrypted backup round trip needs `age` and
is skipped without it; the plain stage is tested either way.
"""

from __future__ import annotations

import base64
import datetime as dt
import gzip
import hashlib
import importlib.machinery
import importlib.util
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
STACK = ROOT / "deploy" / "oracle-stack"
VM = ROOT / "deploy" / "vm"
UNITS = VM / "units"
SHA = "a" * 64


def load_script(name: str, path: Path):
    sys.dont_write_bytecode = True          # do not leave __pycache__ inside deploy/vm
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    loader.exec_module(module)
    return module


def run(cmd: list[str], env: dict[str, str] | None = None, cwd: Path | None = None, check: bool = False) -> subprocess.CompletedProcess:
    full = {**os.environ, "MUSE_VM_CONF": "/nonexistent/vm.conf", "MUSE_PYTHON": sys.executable, **(env or {})}
    full["PATH"] = str(Path(sys.executable).parent) + os.pathsep + full.get("PATH", "")
    return subprocess.run(cmd, env=full, cwd=cwd, capture_output=True, text=True, timeout=120, check=check)


# ------------------------------------------------------------ strict HCL reader

class HclError(ValueError):
    pass


class Block:
    def __init__(self, kind: str, labels: list[str], line: int) -> None:
        self.kind, self.labels, self.line = kind, labels, line
        self.attrs: dict[str, str] = {}
        self.blocks: list[Block] = []

    def find(self, kind: str) -> list["Block"]:
        return [b for b in self.blocks if b.kind == kind]

    def walk(self):
        yield self
        for block in self.blocks:
            yield from block.walk()


_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_-]*")
_PAIRS = {")": "(", "]": "[", "}": "{"}


class _Reader:
    def __init__(self, text: str, name: str) -> None:
        self.s, self.i, self.name = text, 0, name

    def fail(self, message: str) -> None:
        line = self.s.count("\n", 0, self.i) + 1
        raise HclError(f"{self.name}:{line}: {message}")

    def skip(self) -> None:
        s = self.s
        while self.i < len(s):
            if s[self.i] in " \t\r\n":
                self.i += 1
            elif s[self.i] == "#" or s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            elif s.startswith("/*", self.i):
                end = s.find("*/", self.i)
                if end < 0:
                    self.fail("unterminated comment")
                self.i = end + 2
            else:
                return

    def string(self) -> str:
        """A quoted string, with ${...} interpolation that may hold nested strings."""
        s, start = self.s, self.i
        self.i += 1
        while self.i < len(s):
            c = s[self.i]
            if c == "\\":
                self.i += 2
            elif c == '"':
                self.i += 1
                return s[start:self.i]
            elif c == "\n":
                self.fail("newline inside a string")
            elif s.startswith("${", self.i) or s.startswith("%{", self.i):
                self.i += 2
                depth = 1
                while depth and self.i < len(s):
                    if s[self.i] == '"':
                        self.string()
                        continue
                    depth += {"{": 1, "}": -1}.get(s[self.i], 0)
                    self.i += 1
                if depth:
                    self.fail("unterminated interpolation")
            else:
                self.i += 1
        self.fail("unterminated string")
        return ""

    def expression(self) -> str:
        s, start, stack = self.s, self.i, []
        while self.i < len(s):
            c = s[self.i]
            if c == '"':
                self.string()
            elif c == "<" and s.startswith("<<", self.i):
                self.fail("heredocs are not part of the subset this stack uses")
            elif c in "([{":
                stack.append(c)
                self.i += 1
            elif c in ")]}":
                if not stack:
                    break
                if stack.pop() != _PAIRS[c]:
                    self.fail(f"mismatched '{c}'")
                self.i += 1
            elif c == "\n" and not stack:
                break
            elif c == "#" or s.startswith("//", self.i):
                end = s.find("\n", self.i)
                self.i = len(s) if end < 0 else end
            else:
                self.i += 1
        if stack:
            self.fail(f"unclosed '{stack[-1]}'")
        raw = s[start:self.i].strip()
        if not raw:
            self.fail("empty expression")
        return raw

    def body(self, parent: Block, top: bool) -> None:
        s = self.s
        while True:
            self.skip()
            if self.i >= len(s):
                if not top:
                    self.fail("unclosed block")
                return
            if s[self.i] == "}":
                if top:
                    self.fail("unexpected '}'")
                self.i += 1
                return
            match = _IDENT.match(s, self.i)
            if not match:
                self.fail(f"unexpected character {s[self.i]!r}")
            name, line = match.group(0), s.count("\n", 0, self.i) + 1
            self.i = match.end()
            while self.i < len(s) and s[self.i] in " \t":
                self.i += 1
            if self.i < len(s) and s[self.i] == "=" and not s.startswith("==", self.i):
                self.i += 1
                if name in parent.attrs:
                    self.fail(f"attribute '{name}' set twice")
                parent.attrs[name] = self.expression()
                continue
            labels = []
            while self.i < len(s) and s[self.i] != "{":
                if s[self.i] == '"':
                    labels.append(self.string()[1:-1])
                else:
                    label = _IDENT.match(s, self.i)
                    if not label:
                        self.fail(f"expected a block label or '{{' after '{name}'")
                    labels.append(label.group(0))
                    self.i = label.end()
                while self.i < len(s) and s[self.i] in " \t":
                    self.i += 1
            if self.i >= len(s):
                self.fail(f"block '{name}' has no body")
            self.i += 1
            block = Block(name, labels, line)
            parent.blocks.append(block)
            self.body(block, top=False)


def parse_hcl(text: str, name: str = "<text>") -> Block:
    root = Block("<file>", [], 0)
    _Reader(text, name).body(root, top=True)
    return root


def test_hcl_reader_is_strict():
    assert parse_hcl('a = 1\nb "x" {\n  c = merge(\n    { d = "${var.e != "" ? "f" : "g"}" },\n    {}\n  )\n}\n').blocks[0].labels == ["x"]
    for bad in ('a = (1\n', 'b {\n c = 1\n', 'b {\n}\n}\n', 'a = "x\n', 'a = 1\na = 2\n', 'a = [1}\n', 'a =\n', 'b "x" \n', '= 1\n', 'a = "${var.x"\n'):
        with pytest.raises(HclError):
            parse_hcl(bad)


# ------------------------------------------------------------------ Terraform

@pytest.fixture(scope="module")
def tf() -> dict[str, Block]:
    files = sorted(STACK.glob("*.tf"))
    assert files, "no Terraform files"
    return {path.name: parse_hcl(path.read_text(encoding="utf-8"), path.name) for path in files}


def _all_blocks(tf: dict[str, Block]):
    for root in tf.values():
        for top in root.blocks:
            yield from top.walk()


def _top(tf: dict[str, Block], kind: str) -> list[Block]:
    return [b for root in tf.values() for b in root.blocks if b.kind == kind]


def _expressions(block: Block):
    for item in block.walk():
        yield from item.attrs.values()


def test_terraform_files_parse_and_have_the_expected_layout(tf):
    assert {"versions.tf", "variables.tf", "network.tf", "compute.tf", "storage.tf", "iam.tf", "outputs.tf", "release_pin.tf"} <= set(tf)
    allowed = {"terraform", "provider", "variable", "locals", "data", "resource", "output"}
    for name, root in tf.items():
        assert not root.attrs, f"{name}: attributes outside a block"
        assert {b.kind for b in root.blocks} <= allowed, name
    # No provisioners, no modules, no second provider: nothing runs outside Oracle's API.
    kinds = {b.kind for b in _all_blocks(tf)}
    assert not kinds & {"provisioner", "module", "connection"}
    assert [b.labels for b in _top(tf, "provider")] == [["oci"]]


def test_every_reference_is_declared(tf):
    variables = {b.labels[0] for b in _top(tf, "variable")}
    local_names = {name for b in _top(tf, "locals") for name in b.attrs}
    resources = {(b.labels[0], b.labels[1]) for b in _top(tf, "resource")}
    data = {(b.labels[0], b.labels[1]) for b in _top(tf, "data")}
    assert len(variables) == len(_top(tf, "variable")), "a variable is declared twice"
    assert len(resources) == len(_top(tf, "resource")), "a resource is declared twice"
    text = "\n".join(e for b in _all_blocks(tf) for e in b.attrs.values())
    assert set(re.findall(r"\bvar\.([A-Za-z0-9_]+)", text)) <= variables
    assert set(re.findall(r"\blocal\.([A-Za-z0-9_]+)", text)) <= local_names
    assert set(re.findall(r"\bdata\.(oci_[a-z0-9_]+)\.([A-Za-z0-9_]+)", text)) <= data
    used = set(re.findall(r"(?<![.\w])(oci_[a-z0-9_]+)\.([A-Za-z0-9_]+)", text))
    assert used <= resources | data, used - resources - data
    # Unused declarations are usually a mistake too (show_advanced only drives the form).
    assert variables - set(re.findall(r"\bvar\.([A-Za-z0-9_]+)", text)) == {"show_advanced"}
    assert not local_names - set(re.findall(r"\blocal\.([A-Za-z0-9_]+)", text))


def test_provider_is_pinned_exactly(tf):
    terraform = _top(tf, "terraform")
    assert len(terraform) == 1 and "required_version" in terraform[0].attrs
    providers = terraform[0].find("required_providers")[0].attrs
    assert set(providers) == {"oci"}, "only Oracle's provider"
    assert '"oracle/oci"' in providers["oci"]
    assert re.search(r'version\s*=\s*"= \d+\.\d+\.\d+"', providers["oci"]), "the provider must be pinned to one exact version"
    assert not re.search(r"[~>^]", re.search(r'version\s*=\s*"([^"]*)"', providers["oci"]).group(1))


def test_network_lets_nothing_in(tf):
    lists = [b for b in _top(tf, "resource") if b.labels[0] in ("oci_core_security_list", "oci_core_default_security_list", "oci_core_network_security_group_security_rule")]
    assert [b.labels[0] for b in lists].count("oci_core_security_list") == 1
    for block in _all_blocks(tf):
        assert block.kind != "ingress_security_rules", "a static inbound rule"
        assert block.labels[:1] != ["oci_core_network_security_group_security_rule"]
    # The default list every VCN is born with (it allows SSH in) is taken over and left with no rules.
    default = [b for b in lists if b.labels[0] == "oci_core_default_security_list"]
    assert len(default) == 1 and not default[0].blocks and "manage_default_resource_id" in default[0].attrs
    # One conditional inbound rule exists, for an early tester's SSH, and it is empty by default.
    main = [b for b in lists if b.labels[0] == "oci_core_security_list"][0]
    dynamic = main.find("dynamic")
    assert [d.labels for d in dynamic] == [["ingress_security_rules"]]
    assert dynamic[0].attrs["for_each"] == "local.tester_ssh ? [var.ssh_source_cidr] : []"
    content = dynamic[0].find("content")[0]
    assert content.attrs["source"] == "ingress_security_rules.value"
    assert content.find("tcp_options")[0].attrs == {"min": "22", "max": "22"}
    locals_ = {k: v for b in _top(tf, "locals") for k, v in b.attrs.items()}
    assert locals_["tester_ssh"] == 'var.ssh_public_key != "" && var.ssh_source_cidr != ""'
    variables = {b.labels[0]: b for b in _top(tf, "variable")}
    assert variables["ssh_public_key"].attrs["default"] == '""' and variables["ssh_source_cidr"].attrs["default"] == '""'
    # "Anywhere" can never be the source: the variable refuses prefixes shorter than /24.
    condition = variables["ssh_source_cidr"].find("validation")[0].attrs["condition"]
    assert "/([0-9]|1[0-9]|2[0-3])$" in condition and condition.count("!can(regex(") == 1
    assert re.search(r"/([0-9]|1[0-9]|2[0-3])$", "0.0.0.0/0") and not re.search(r"/([0-9]|1[0-9]|2[0-3])$", "203.0.113.7/32")
    # 0.0.0.0/0 appears only as a destination (outbound rules and the route), never as a source.
    for block in _all_blocks(tf):
        for key, value in block.attrs.items():
            if "0.0.0.0/0" in value and key not in ("error_message", "description"):
                assert key == "destination" and block.kind in ("egress_security_rules", "route_rules"), (block.kind, key)
    # Outbound is limited to HTTPS and HTTP, stateful.
    egress = main.find("egress_security_rules")
    assert sorted(e.find("tcp_options")[0].attrs["min"] for e in egress) == ["443", "80"]
    assert all(e.attrs["protocol"] == '"6"' and e.attrs["stateless"] == "false" for e in egress)
    subnet = [b for b in _top(tf, "resource") if b.labels[0] == "oci_core_subnet"][0]
    assert subnet.attrs["security_list_ids"] == "[oci_core_security_list.muse.id]"
    # No load balancer, no reserved public address, no NAT or service gateway: nothing else that could face the internet.
    types = {b.labels[0] for b in _top(tf, "resource")}
    assert types == {"oci_core_vcn", "oci_core_internet_gateway", "oci_core_route_table", "oci_core_security_list",
                     "oci_core_default_security_list", "oci_core_subnet", "oci_core_instance", "oci_objectstorage_bucket",
                     "oci_objectstorage_object_lifecycle_policy", "oci_identity_dynamic_group", "oci_identity_policy"}


def test_instance_is_two_cores_twelve_gb_by_default(tf):
    variables = {b.labels[0]: b for b in _top(tf, "variable")}
    assert variables["ocpus"].attrs["default"] == "2" and variables["memory_in_gbs"].attrs["default"] == "12"
    assert "var.ocpus <= 2" in variables["ocpus"].find("validation")[0].attrs["condition"]
    assert "var.memory_in_gbs <= 12" in variables["memory_in_gbs"].find("validation")[0].attrs["condition"]
    assert variables["boot_volume_size_in_gbs"].attrs["default"] == "50"
    assert "<= 200" in variables["boot_volume_size_in_gbs"].find("validation")[0].attrs["condition"]
    assert variables["time_zone"].attrs["default"] == '"Asia/Taipei"'
    instance = [b for b in _top(tf, "resource") if b.labels[0] == "oci_core_instance"]
    assert len(instance) == 1
    inst = instance[0]
    assert inst.attrs["shape"] == "local.shape"
    locals_ = {k: v for b in _top(tf, "locals") for k, v in b.attrs.items()}
    assert locals_["shape"] == '"VM.Standard.A1.Flex"'
    assert inst.find("shape_config")[0].attrs == {"ocpus": "var.ocpus", "memory_in_gbs": "var.memory_in_gbs"}
    assert inst.find("instance_options")[0].attrs == {"are_legacy_imds_endpoints_disabled": "true"}
    image = [b for b in _top(tf, "data") if b.labels[0] == "oci_core_images"][0]
    assert image.attrs["operating_system"] == '"Canonical Ubuntu"' and image.attrs["operating_system_version"] == '"24.04"'
    assert image.attrs["shape"] == "local.shape"
    # No SSH key unless a tester gave both a key and a source address.
    assert "local.tester_ssh ? { ssh_authorized_keys = var.ssh_public_key } : {}" in inst.attrs["metadata"]
    assert "base64gzip(local.user_data)" in inst.attrs["metadata"]
    # The release is pinned: a plan fails without a fixed version and a checksum.
    conditions = [p.attrs["condition"] for p in inst.find("lifecycle")[0].find("precondition")]
    assert any("local.release_sha256" in c and "[0-9a-f]{64}" in c for c in conditions)
    assert any("local.release_version" in c for c in conditions)
    assert any("https://" in c for c in conditions)
    pin = {k: v for k, v in [b for b in tf["release_pin.tf"].blocks if b.kind == "locals"][0].attrs.items()}
    assert set(re.findall(r'(\w+)\s*=\s*""', pin["pinned_release"])) == {"version", "url", "sha256"}, "the repository copy must not carry a release"


def test_backups_bucket_and_identity(tf):
    resources = {b.labels[0]: b for b in _top(tf, "resource")}
    bucket = resources["oci_objectstorage_bucket"]
    assert bucket.attrs["access_type"] == '"NoPublicAccess"' and bucket.attrs["versioning"] == '"Disabled"'
    assert bucket.attrs["count"] == "local.bucket_enabled ? 1 : 0"
    rules = resources["oci_objectstorage_object_lifecycle_policy"].find("rules")
    assert {r.attrs["action"] for r in rules} == {'"DELETE"', '"ABORT"'}
    group, policy = resources["oci_identity_dynamic_group"], resources["oci_identity_policy"]
    assert group.attrs["count"] == policy.attrs["count"] == "local.identity_enabled ? 1 : 0"
    # Exactly this instance, and only the backup bucket.
    assert group.attrs["matching_rule"] == "\"instance.id = '${oci_core_instance.muse.id}'\""
    statements = re.findall(r'"(Allow [^"]*(?:"[^"]*)*?)",?\n', policy.attrs["statements"] + "\n")
    assert len(statements) == 3
    for statement in statements[:2]:
        assert "dynamic-group" in statement and "target.bucket.name='${local.bucket_name}'" in statement
        assert " in tenancy" not in statement.replace("${local.policy_scope}", "")
    assert "to read buckets" in statements[0] and "to manage objects" in statements[1]
    assert "manage all-resources" not in policy.attrs["statements"] and "any-user" not in policy.attrs["statements"]
    variables = {b.labels[0]: b for b in _top(tf, "variable")}
    assert variables["create_identity_resources"].attrs["type"] == "bool"


def test_outputs_hold_no_secrets(tf):
    outputs = {b.labels[0]: b for b in _top(tf, "output")}
    assert "instance_ocid" in outputs
    for name, block in outputs.items():
        text = (name + " " + " ".join(block.attrs.values())).lower()
        for word in ("password", "secret", "token", "private_key", "master", "user_data", "metadata", "api_key", "identity.txt"):
            assert word not in text, f"output {name} mentions {word}"
        assert "sensitive" not in block.attrs, "nothing sensitive should be an output at all"
    # No variable is a secret either: Resource Manager stores them in the stack.
    for block in _top(tf, "variable"):
        assert not re.search(r"password|secret|token|private|master_key|api_key", block.labels[0]), block.labels[0]
        assert "sensitive" not in block.attrs


# ----------------------------------------------------------------- schema.yaml

def test_schema_is_valid_and_matches_the_variables(tf):
    schema = yaml.safe_load((STACK / "schema.yaml").read_text(encoding="utf-8"))
    assert schema["schemaVersion"] in ("1.0.0", "1.1.0") and schema["title"]
    variables = {b.labels[0]: b for b in _top(tf, "variable")}
    outputs = {b.labels[0] for b in _top(tf, "output")}
    assert set(schema["variables"]) <= set(variables), set(schema["variables"]) - set(variables)
    grouped = [name for group in schema["variableGroups"] for name in group["variables"]]
    assert len(grouped) == len(set(grouped)) and set(grouped) == set(variables), "every variable sits in exactly one group"
    assert set(schema["outputs"]) <= outputs
    for group in schema["outputGroups"]:
        for ref in group["outputs"]:
            assert re.fullmatch(r"\$\{(\w+)\}", ref) and ref[2:-1] in outputs
    # The person must choose only where it goes; everything else has a default.
    required = {name for name, spec in schema["variables"].items() if spec.get("required")}
    assert required == {"region", "compartment_ocid", "availability_domain"}
    no_default = {name for name, block in variables.items() if "default" not in block.attrs}
    assert no_default == required | {"tenancy_ocid"}
    assert schema["variables"]["region"]["type"] == "oci:identity:region:name"
    assert schema["variables"]["compartment_ocid"]["type"] == "oci:identity:compartment:id"
    ad = schema["variables"]["availability_domain"]
    assert ad["type"] == "oci:identity:availabilitydomain:name" and ad["dependsOn"] == {"compartmentId": "${compartment_ocid}"}
    # Schema types agree with Terraform types, and defaults agree with Terraform defaults.
    kinds = {"string": "string", "boolean": "bool", "integer": "number"}
    for name, spec in schema["variables"].items():
        if spec["type"] in kinds:
            assert variables[name].attrs["type"] == kinds[spec["type"]], name
        if "default" in spec:
            assert json.dumps(spec["default"]) == variables[name].attrs["default"], name
    for name, spec in schema["variables"].items():
        if isinstance(spec.get("visible"), str):
            assert spec["visible"] in variables
    assert schema["variables"]["time_zone"]["default"] == "Asia/Taipei"


# ------------------------------------------------------------------ cloud-init

SAMPLE = {
    "muse_version": "0.2.0", "release_url": "https://releases.example/muse-0.2.0.tar.gz", "release_sha256": SHA,
    "time_zone": "Asia/Taipei", "users_block": "users: []", "tester_ssh": "no", "backup": "on",
    "backup_bucket": "muse-backups-abc123", "backup_namespace": "examplens", "backup_keep": "14", "account_type": "always_free", "idle_guard": "on",
    "ocpus": "2", "memory_gb": "12", "update_manifest_url": "", "update_public_key": "",
}


def render_template(values: dict[str, str]) -> str:
    """What Terraform's templatefile() does for the subset the template uses: ${name} only."""
    text = (STACK / "cloud-init.yaml.tftpl").read_text(encoding="utf-8")
    assert "%{" not in text and "$${" not in text, "the template uses only plain ${name} substitution"
    names = set(re.findall(r"\$\{([^}]*)\}", text))
    assert names == set(values), names ^ set(values)
    return re.sub(r"\$\{(\w+)\}", lambda m: str(values[m.group(1)]), text)


def first_boot_script(values: dict[str, str]) -> str:
    doc = yaml.safe_load(render_template(values))
    return next(f["content"] for f in doc["write_files"] if f["path"] == "/usr/local/sbin/muse-first-boot")


def test_template_variables_match_what_terraform_passes(tf):
    locals_ = {k: v for b in _top(tf, "locals") for k, v in b.attrs.items()}
    call = locals_["user_data"]
    assert call.startswith('templatefile("${path.module}/cloud-init.yaml.tftpl"')
    passed = set(re.findall(r"^\s*(\w+)\s*=", call.split("{", 2)[2], re.MULTILINE))
    assert passed == set(SAMPLE)


def test_cloud_init_renders_to_valid_yaml_and_is_small():
    text = render_template(SAMPLE)
    assert text.startswith("#cloud-config\n")
    doc = yaml.safe_load(text)
    assert doc["timezone"] == "Asia/Taipei" and doc["ssh_pwauth"] is False and doc["users"] == []
    assert doc["runcmd"] == [["/usr/local/sbin/muse-first-boot"]]
    assert {f["path"] for f in doc["write_files"]} == {"/etc/muse/vm.conf", "/usr/local/sbin/muse-first-boot"}
    tester = yaml.safe_load(render_template({**SAMPLE, "users_block": "users:\n  - default", "tester_ssh": "yes"}))
    assert tester["users"] == ["default"]
    # Size: cloud-init's commonly quoted limit is 16 KB; Oracle allows 32,000 bytes of metadata in all.
    # Worst case: long values, uncompressed and after gzip + base64 (what compute.tf sends).
    big = render_template({**SAMPLE, "release_url": "https://releases.example/" + "x" * 400, "update_manifest_url": "https://u.example/" + "y" * 300,
                           "update_public_key": "A" * 44, "users_block": "users:\n  - default"})
    raw = len(big.encode())
    packed = len(base64.b64encode(gzip.compress(big.encode(), mtime=0)))
    assert raw < 16 * 1024, raw
    assert packed < 16 * 1024 and packed < raw, packed
    print(f"user_data: {len(text.encode())} bytes rendered, {len(base64.b64encode(gzip.compress(text.encode(), mtime=0)))} after gzip+base64")


def test_cloud_init_settings_file_has_no_secret():
    doc = yaml.safe_load(render_template(SAMPLE))
    conf = next(f for f in doc["write_files"] if f["path"] == "/etc/muse/vm.conf")
    keys = [line.split("=", 1)[0] for line in conf["content"].splitlines() if line and not line.startswith("#")]
    assert len(keys) == len(set(keys))
    for key in keys:
        assert not re.search(r"MASTER|SECRET|PASSWORD|API_KEY|TOKEN|IDENTITY", key), key
    assert "MUSE_UPDATE_PUBKEY" in keys and "MUSE_BACKUP_RECIPIENT" in keys   # public keys only
    text = render_template(SAMPLE)
    assert "LITELLM_MASTER_KEY" not in text and "sk-" not in text and "openssl" not in text
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
    assert "git " not in code and not re.search(r"\bpip3?\b", code) and "docker" not in code.lower()


SECRET_NAMES = re.compile(r"\$\{?(key|KEY|LITELLM_MASTER_KEY|MASTER|RECIPIENT_PRIVATE|identity)\b")


def shell_sources() -> dict[str, str]:
    out = {p.name: p.read_text(encoding="utf-8") for p in sorted(VM.glob("*.sh"))}
    out["build-zip.sh"] = (STACK / "build-zip.sh").read_text(encoding="utf-8")
    out["muse-first-boot"] = first_boot_script(SAMPLE)
    out["oracle-a1-setup.sh"] = (ROOT / "deploy" / "oracle-a1-setup.sh").read_text(encoding="utf-8")
    return out


def test_no_script_traces_or_prints_a_secret():
    for name, text in shell_sources().items():
        code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#"))
        assert not re.search(r"\bset\s+-[a-wyz]*x|\bset\s+-o\s+xtrace|bash\s+-[a-wyz]*x\b|^#!.*\s-[a-wyz]*x", code, re.MULTILINE), f"{name} turns on tracing"
        for line in code.splitlines():
            if re.search(r"\b(echo|say|warn|die|printf)\b", line) and SECRET_NAMES.search(line):
                # The one allowed use: a builtin printf straight into the 0600 dotenv file, no pipe, no terminal.
                assert re.search(r"^\s*printf '[^']*' (\"\$\w+\" ?)+>> (\"\$ENV_FILE\"|\.env)$", line), f"{name}: {line.strip()}"
            assert "--show-master-key" not in line or name == "oracle-a1-setup.sh", name
    boot = (VM / "bootstrap.sh").read_text(encoding="utf-8")
    start = boot.index("do_secrets() {")
    secrets = boot[start:boot.index("\n}\n", start)]
    assert "umask 077" in secrets and 'chmod 600 "$ENV_FILE"' in secrets and "openssl rand -hex 32" in secrets
    assert secrets.count("$key") == 1 and secrets.count("${#key}") == 1 and 'key=""' in secrets   # written once; only its length is checked
    assert not re.search(r"(echo|say|tee|logger|cat)[^\n]*\$key", secrets)
    assert "age-keygen -o" in boot and "2>/dev/null" in boot.split("age-keygen -o", 1)[1].splitlines()[0]
    for name in ("oci_object.py", "backup_tool.py", "update_verify.py", "muse-vm-monitor.py", "muse-vm-status"):
        text = (VM / name).read_text(encoding="utf-8")
        assert not re.search(r"print\([^)]*(token|leaf_key|session_key|key_pem)", text), name


def make_release(tmp_path: Path, marker: Path) -> tuple[Path, str]:
    """A stand-in release tarball whose bootstrap.sh only leaves a marker."""
    tree = tmp_path / "build" / "muse-0.2.0" / "deploy" / "vm"
    tree.mkdir(parents=True)
    (tree / "bootstrap.sh").write_text(f"#!/bin/bash\necho ran > {marker}\n", encoding="utf-8")
    tarball = tmp_path / "release.tar.gz"
    with tarfile.open(tarball, "w:gz") as tar:
        tar.add(tmp_path / "build" / "muse-0.2.0", arcname="muse-0.2.0")
    return tarball, hashlib.sha256(tarball.read_bytes()).hexdigest()


def run_first_boot(tmp_path: Path, values: dict[str, str], tarball: Path | None) -> subprocess.CompletedProcess:
    script = first_boot_script(values)
    script = script.replace("SETUP=/var/lib/muse-setup", f"SETUP={tmp_path}/setup").replace("RELEASES=/opt/muse/releases", f"RELEASES={tmp_path}/releases")
    path = tmp_path / "first-boot.sh"
    path.write_text(script, encoding="utf-8")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir(exist_ok=True)
    # A stand-in for curl: no network. It copies the local tarball to --output, or fails.
    (fake_bin / "curl").write_text(
        "#!/bin/bash\nout=''\nwhile [ $# -gt 0 ]; do [ \"$1\" = --output ] && { shift; out=\"$1\"; }; shift; done\n"
        + (f"cp '{tarball}' \"$out\"\n" if tarball else "exit 22\n"), encoding="utf-8")
    (fake_bin / "curl").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"}
    return subprocess.run(["bash", str(path)], env=env, capture_output=True, text=True, timeout=60)


def test_first_boot_installs_only_a_pinned_verified_release(tmp_path):
    marker = tmp_path / "bootstrap-ran"
    tarball, digest = make_release(tmp_path, marker)

    # A wrong checksum: refuse, leave a marker, never run anything from the download.
    wrong = run_first_boot(tmp_path, {**SAMPLE, "release_sha256": "b" * 64}, tarball)
    assert wrong.returncode == 1 and "does not match its pinned SHA-256" in wrong.stderr
    assert "does not match" in (tmp_path / "setup" / "FAILED").read_text()
    assert not marker.exists() and not (tmp_path / "releases" / "0.2.0").exists()

    # A failed download.
    assert run_first_boot(tmp_path, {**SAMPLE, "release_sha256": digest}, None).returncode == 1
    assert not marker.exists()

    # The right checksum: unpack to releases/<version> and hand over to its bootstrap.sh.
    good = run_first_boot(tmp_path, {**SAMPLE, "release_sha256": digest}, tarball)
    assert good.returncode == 0, good.stderr
    assert marker.read_text().strip() == "ran"
    assert (tmp_path / "releases" / "0.2.0" / "deploy" / "vm" / "bootstrap.sh").is_file()
    assert not (tmp_path / "setup" / "FAILED").exists()


@pytest.mark.parametrize("version", ["main", "latest", "master", "HEAD", "", "stable", "1.2", "v1", "0.2.0; rm -rf x", "refs/heads/main"])
def test_first_boot_refuses_a_floating_ref(tmp_path, version):
    marker = tmp_path / "bootstrap-ran"
    tarball, digest = make_release(tmp_path, marker)
    if "'" in version or ";" in version:
        version_values = {**SAMPLE, "muse_version": version.replace("'", "")}
    else:
        version_values = {**SAMPLE, "muse_version": version}
    result = run_first_boot(tmp_path, {**version_values, "release_sha256": digest}, tarball)
    assert result.returncode == 1 and "not a fixed release" in result.stderr
    assert not marker.exists() and (tmp_path / "setup" / "FAILED").is_file()


def test_first_boot_refuses_a_missing_checksum_and_plain_http(tmp_path):
    marker = tmp_path / "bootstrap-ran"
    tarball, digest = make_release(tmp_path, marker)
    assert "no pinned SHA-256" in run_first_boot(tmp_path, {**SAMPLE, "release_sha256": ""}, tarball).stderr
    result = run_first_boot(tmp_path, {**SAMPLE, "release_sha256": digest, "release_url": "http://releases.example/m.tar.gz"}, tarball)
    assert result.returncode == 1 and "not https" in result.stderr and not marker.exists()
    text = first_boot_script(SAMPLE)
    assert "--proto '=https'" in text and "sha256sum --check" in text and "git " not in text


def test_lib_version_and_unpack_rules(tmp_path):
    def sh(snippet: str) -> subprocess.CompletedProcess:
        return run(["bash", "-c", f'. "{VM}/lib.sh"; {snippet}'])

    for good in ("0.2.0", "v1.10.3", "2.0.0-rc.1"):
        assert sh(f"valid_version '{good}'").returncode == 0, good
    for bad in ("main", "latest", "", "1.2", "stable", "HEAD", "0.2.0/../x", "nightly"):
        assert sh(f"valid_version '{bad}'").returncode != 0, bad
    assert sh(f"valid_sha256 {SHA}").returncode == 0 and sh("valid_sha256 abc").returncode != 0
    assert sh("fetch_https http://example.invalid/x /dev/null").returncode != 0
    # A release archive that climbs out of its folder, or holds two top-level folders, is refused.
    evil = tmp_path / "evil.tar.gz"
    with tarfile.open(evil, "w:gz") as tar:
        info = tarfile.TarInfo("muse-0.2.0/../../escape")
        info.size = 0
        tar.addfile(info)
    result = sh(f"unpack_release '{evil}' 0.2.0 '{tmp_path}/rel'")
    assert result.returncode != 0 and "unsafe path" in result.stderr
    assert not (tmp_path / "escape").exists()
    marker = tmp_path / "m"
    tarball, _ = make_release(tmp_path, marker)
    assert sh(f"unpack_release '{tarball}' 0.2.0 '{tmp_path}/releases' && switch_release '{tmp_path}' 0.2.0 && [ \"$(current_release '{tmp_path}')\" = 0.2.0 ]").returncode == 0
    conf = tmp_path / "vm.conf"
    conf.write_text("# c\nA=1\nB=$(touch " + str(tmp_path / "pwned") + ")\nbad key=2\n", encoding="utf-8")
    out = sh(f"load_conf '{conf}'; printf '%s|%s' \"$A\" \"$B\"")
    assert out.stdout.startswith("1|$(touch") and not (tmp_path / "pwned").exists(), "settings are parsed, never executed"


# ------------------------------------------------- first boot, as a dry run

STUBS = {
    # Each stands in for a system command so that bootstrap.sh can run on a test machine: nothing is installed,
    # no account is made, no unit is started. Calls are logged to $STUB_LOG.
    "id": '#!/bin/bash\ncase "$*" in -u) echo 0 ;; -un) echo root ;; "-nG muse") echo muse ;; esac\nexit 0\n',
    "apt-get": '#!/bin/bash\necho "apt-get $*" >> "$STUB_LOG"\n',
    "passwd": '#!/bin/bash\necho "passwd $*" >> "$STUB_LOG"\n',
    "useradd": '#!/bin/bash\necho "useradd $*" >> "$STUB_LOG"\n',
    "sysctl": "#!/bin/bash\nexit 0\n",
    "timedatectl": '#!/bin/bash\necho "timedatectl $*" >> "$STUB_LOG"\n',
    "chown": "#!/bin/bash\nexit 0\n",
    "systemctl": '#!/bin/bash\necho "systemctl $*" >> "$STUB_LOG"\n',
    "runuser": '#!/bin/bash\nshift 3\nexec "$@"\n',
    "install": '#!/bin/bash\nargs=()\nwhile [ $# -gt 0 ]; do case "$1" in -o|-g) shift ;; *) args+=("$1") ;; esac; shift; done\nexec /usr/bin/install "${args[@]}"\n',
    "curl": "#!/bin/bash\nexit 0\n",
    "ss": '#!/bin/bash\necho "LISTEN 0 4096 127.0.0.1:4000 0.0.0.0:*"\n[ -f "$STUB_DIR/extra-listener" ] && cat "$STUB_DIR/extra-listener"\nexit 0\n',
    # `python3 -m venv DIR` makes a stand-in interpreter that accepts everything (or fails, when asked to).
    "python3": '#!/bin/bash\nif [ "$1" = -m ] && [ "$2" = venv ]; then mkdir -p "$3/bin"\n'
               '  printf \'#!/bin/bash\\n[ -f "%s/python-fails" ] && exit 1\\nexit 0\\n\' "$STUB_DIR" > "$3/bin/python"; chmod +x "$3/bin/python"; exit 0\nfi\n'
               'exec "$REAL_PYTHON" "$@"\n',
}


def dry_boot(tmp_path: Path, **settings: str) -> tuple[subprocess.CompletedProcess, dict[str, Path]]:
    stubs = tmp_path / "stubs"
    if not stubs.exists():
        stubs.mkdir()
        for name, body in STUBS.items():
            (stubs / name).write_text(body, encoding="utf-8")
            (stubs / name).chmod(0o755)
        if not AGE:
            (stubs / "age-keygen").write_text('#!/bin/bash\nif [ "$1" = -o ]; then echo "# created" > "$2"; else echo age1standin; fi\n', encoding="utf-8")
            (stubs / "age-keygen").chmod(0o755)
        release = tmp_path / "opt" / "muse" / "releases" / "0.2.0"
        shutil.copytree(VM, release / "deploy" / "vm", ignore=shutil.ignore_patterns("__pycache__"))
        (release / "requirements").mkdir()
        shutil.copy(ROOT / "requirements" / "proxy.lock", release / "requirements" / "proxy.lock")
        (release / "scripts").mkdir()
        shutil.copy(ROOT / "scripts" / "verify_litellm.py", release / "scripts" / "verify_litellm.py")
    paths = {
        "base": tmp_path / "opt" / "muse", "state": tmp_path / "var" / "lib" / "muse", "setup": tmp_path / "setup",
        "sysroot": tmp_path / "sysroot", "log": tmp_path / "calls.log", "stubs": stubs,
    }
    paths["env"] = paths["state"] / "muse.env"
    env = {
        **os.environ, "PATH": f"{stubs}{os.pathsep}{os.environ['PATH']}", "STUB_LOG": str(paths["log"]), "STUB_DIR": str(stubs),
        "REAL_PYTHON": sys.executable, "MUSE_VM_CONF": "/nonexistent/vm.conf", "MUSE_BASE": str(paths["base"]),
        "MUSE_STATE_ROOT": str(paths["state"]), "MUSE_HOME": str(paths["state"] / "home"), "MUSE_VM_STATE": str(paths["state"] / "vm"),
        "MUSE_ENV_FILE": str(paths["env"]), "MUSE_SETUP_DIR": str(paths["setup"]), "MUSE_SYSROOT": str(paths["sysroot"]),
        "MUSE_TZ": "Asia/Taipei", **settings,
    }
    script = paths["base"] / "releases" / "0.2.0" / "deploy" / "vm" / "bootstrap.sh"
    return subprocess.run(["bash", str(script)], env=env, capture_output=True, text=True, timeout=120), paths


def test_first_boot_dry_run_sets_everything_up_once_and_prints_no_secret(tmp_path):
    result, paths = dry_boot(tmp_path)
    assert result.returncode == 0, result.stdout + result.stderr
    assert (paths["setup"] / "DONE").read_text().startswith("version=0.2.0") and not (paths["setup"] / "FAILED").exists()
    assert os.readlink(paths["base"] / "current") == "releases/0.2.0"
    env_text = paths["env"].read_text()
    key = re.search(r"^LITELLM_MASTER_KEY=(sk-muse-[0-9a-f]{64})$", env_text, re.MULTILINE).group(1)
    assert (paths["env"].stat().st_mode & 0o777) == 0o600
    assert "MUSE_BIND=127.0.0.1" in env_text and "MUSE_TZ=Asia/Taipei" in env_text
    output = result.stdout + result.stderr
    assert key not in output and key[8:24] not in output and "sk-muse" not in output and "AGE-SECRET-KEY" not in output
    assert "master key: generated and stored (not shown)" in output
    for path in tmp_path.rglob("*"):                      # the key is in exactly one file
        if path.is_file() and path != paths["env"] and path.stat().st_size < 1_000_000:
            assert key.encode() not in path.read_bytes(), path
    units = paths["sysroot"] / "etc" / "systemd" / "system"
    assert {p.name for p in units.iterdir()} == {p.name for p in unit_files()}
    calls = paths["log"].read_text()
    for unit in ("muse-proxy.service", "muse-home.service", "muse-queue.service", "muse-vm-monitor.service", "muse-chores.timer", "muse-backup.timer"):
        assert f"systemctl enable --now {unit}" in calls, unit
    assert "systemctl enable --now muse-chores.service" not in calls, "timer-driven services are started by their timers"
    assert "systemctl mask ssh.socket ssh.service" in calls, "no tester key: sshd is switched off"
    assert "apt-get install -y -qq --no-install-recommends python3-venv age curl ca-certificates unattended-upgrades" in calls
    assert "docker" not in calls and "useradd" not in calls
    apt = (paths["sysroot"] / "etc" / "apt" / "apt.conf.d" / "52muse-unattended-upgrades").read_text()
    assert 'Automatic-Reboot "true"' in apt and 'Automatic-Reboot-Time "03:30"' in apt
    wrapper = (paths["sysroot"] / "usr" / "local" / "bin" / "muse").read_text()
    assert "MUSE_BIND=127.0.0.1" in wrapper and "-m muse" in wrapper and key not in wrapper
    assert (paths["state"] / "home" / "backup-key" / "identity.txt").is_file()
    assert (paths["state"].stat().st_mode & 0o777) == 0o700

    # A second run changes nothing and keeps the same key.
    again, _ = dry_boot(tmp_path)
    assert again.returncode == 0 and "secrets: already done" in again.stdout and "python: already done" in again.stdout
    assert paths["env"].read_text() == env_text


def test_first_boot_failure_leaves_a_marker_and_nothing_running(tmp_path):
    # The install does not pass its checks: stop before any secret or unit exists.
    (tmp_path / "pre").mkdir()
    first, paths = dry_boot(tmp_path / "pre")
    assert first.returncode == 0
    broken = tmp_path / "broken"
    broken.mkdir()
    dry_boot(broken)                                         # builds the stand-in tree
    shutil.rmtree(broken / "setup")
    shutil.rmtree(broken / "var")
    shutil.rmtree(broken / "sysroot")
    (broken / "calls.log").unlink()
    (broken / "stubs" / "python-fails").write_text("")
    shutil.rmtree(broken / "opt" / "muse" / "releases" / "0.2.0" / "venv")
    failed, paths = dry_boot(broken)
    assert failed.returncode != 0
    marker = (paths["setup"] / "FAILED").read_text()
    assert "step=python" in marker and not (paths["setup"] / "DONE").exists()
    assert not paths["env"].exists(), "no key is made when the install fails"
    assert not (paths["sysroot"] / "etc" / "systemd" / "system").exists(), "no unit is installed"
    assert "systemctl disable --now" in paths["log"].read_text() and "enable --now muse-proxy" not in paths["log"].read_text()
    assert 'stopped at step "python"' in failed.stderr

    # Something listens beyond loopback after the services start: everything is stopped and disabled.
    exposed = tmp_path / "exposed"
    exposed.mkdir()
    dry_boot(exposed)
    shutil.rmtree(exposed / "setup")
    (exposed / "calls.log").unlink()
    (exposed / "stubs" / "extra-listener").write_text("LISTEN 0 128 0.0.0.0:8080 0.0.0.0:*\n")
    result, paths = dry_boot(exposed)
    assert result.returncode != 0 and "listening beyond loopback: 0.0.0.0:8080" in result.stderr
    assert "step=health" in (paths["setup"] / "FAILED").read_text() and not (paths["setup"] / "DONE").exists()
    calls = paths["log"].read_text()
    assert calls.rindex("systemctl disable --now") > calls.rindex("systemctl enable --now"), "the last word is: stop everything"
    assert all(unit in calls.split("systemctl disable --now")[-1] for unit in ("muse-proxy.service", "muse-home.service", "muse-queue.service", "muse-chores.timer"))

    # sshd on port 22 is tolerated only when a tester key was given.
    (exposed / "stubs" / "extra-listener").write_text("LISTEN 0 128 0.0.0.0:22 0.0.0.0:*\n")
    shutil.rmtree(exposed / "setup")
    assert dry_boot(exposed)[0].returncode != 0
    shutil.rmtree(exposed / "setup")
    (exposed / "calls.log").unlink()
    tester, paths = dry_boot(exposed, MUSE_TESTER_SSH="yes")
    assert tester.returncode == 0, tester.stderr
    assert "mask ssh" not in paths["log"].read_text()


# -------------------------------------------------------------- systemd units

HARDENING = ["NoNewPrivileges=yes", "ProtectSystem=strict", "PrivateTmp=yes", "ProtectHome=", "ReadWritePaths=", "RestrictAddressFamilies=",
             "MemoryMax=", "CPUQuota="]
LOCAL_ONLY = {"muse-home.service", "muse-queue.service", "muse-chores.service", "muse-vm-monitor.service"}
NEEDS_INTERNET = {"muse-proxy.service", "muse-catalog-check.service", "muse-backup.service", "muse-update.service"}


def unit_files() -> list[Path]:
    return sorted(UNITS.glob("*"))


def unit_lines(path: Path) -> list[str]:
    return [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip() and not line.lstrip().startswith("#")]


def test_expected_units_exist():
    names = {p.name for p in unit_files()}
    assert names == {"muse-proxy.service", "muse-home.service", "muse-queue.service", "muse-chores.service", "muse-chores.timer",
                     "muse-catalog-check.service", "muse-catalog-check.timer", "muse-backup.service", "muse-backup.timer",
                     "muse-update.service", "muse-update.timer", "muse-vm-monitor.service"}
    assert LOCAL_ONLY | NEEDS_INTERNET == {n for n in names if n.endswith(".service")}
    listed = re.search(r'ALL="([^"]*)"', (VM / "install-services.sh").read_text()).group(1).split()
    assert set(listed) == names


def test_every_service_is_hardened():
    for path in unit_files():
        if path.suffix != ".service":
            continue
        lines = unit_lines(path)
        for directive in HARDENING:
            assert any(line.startswith(directive) for line in lines), f"{path.name} lacks {directive}"
        value = {line.split("=", 1)[0]: line.split("=", 1)[1] for line in lines if "=" in line}
        assert "ProtectSystem=strict" in lines
        assert value["ProtectHome"] == "@PROTECT_HOME@"
        assert "@MUSE_STATE@" in value["ReadWritePaths"]
        memory = value["MemoryMax"]
        assert re.fullmatch(r"\d+[MG]", memory)
        assert "AF_PACKET" not in value["RestrictAddressFamilies"]
        if path.name in LOCAL_ONLY:
            assert "IPAddressDeny=any" in lines, f"{path.name} needs no internet and must not have it"
            if path.name == "muse-vm-monitor.service":
                assert "IPAddressAllow=localhost" not in lines and value["RestrictAddressFamilies"] == "AF_UNIX"
            else:
                assert "IPAddressAllow=localhost" in lines
        else:
            assert "IPAddressDeny=any" not in lines
        if path.name == "muse-update.service":
            assert "User=" not in "".join(lines), "the updater installs under /opt and restarts units, so it is root"
        else:
            assert "User=@MUSE_USER@" in lines and "CapabilityBoundingSet=" in lines
            assert value["ReadWritePaths"] == "@MUSE_STATE@", "one writable folder only"
    # Sized for 12 GB: everything that can run at once stays well under it.
    def gb(name: str) -> float:
        text = next(line for line in unit_lines(UNITS / name) if line.startswith("MemoryMax=")).split("=")[1]
        return float(text[:-1]) / (1024 if text.endswith("M") else 1)
    assert sum(gb(n) for n in ("muse-proxy.service", "muse-home.service", "muse-queue.service", "muse-chores.service", "muse-vm-monitor.service")) <= 10.0
    assert sum(gb(p.name) for p in unit_files() if p.suffix == ".service") <= 12.0 + 2.0   # the nightly jobs never all overlap


def test_nothing_binds_publicly():
    for path in list(unit_files()) + list(VM.glob("*.sh")) + list(VM.glob("*.py")) + [VM / "muse-vm-status", STACK / "cloud-init.yaml.tftpl"]:
        code = "\n".join(line for line in path.read_text(encoding="utf-8").splitlines() if not line.lstrip().startswith(("#", "//")))
        assert "0.0.0.0" not in code, path.name
        assert not re.search(r"(?<![\w:\[])::(?![\w:1])|\[::\](?!1)", code.replace("[::1]", "").replace("[::ffff:127.", "")), path.name
        assert "--host" not in code and "INADDR_ANY" not in code, path.name
    proxy = unit_lines(UNITS / "muse-proxy.service")
    assert "Environment=MUSE_BIND=127.0.0.1" in proxy, "the unit pins loopback even if the dotenv file were edited"
    home = unit_lines(UNITS / "muse-home.service")
    assert any(line.endswith("-m muse home serve") for line in home), "the home API uses its Unix socket, no --tcp"
    boot = (VM / "bootstrap.sh").read_text(encoding="utf-8")
    assert 'only_loopback_listeners "$TESTER_SSH" || die' in boot
    assert '[ "$bind" = "127.0.0.1" ] || die' in boot


def test_units_call_commands_the_cli_really_has():
    from muse.cli import build_parser

    parser = build_parser()
    seen = []
    for path in unit_files():
        for line in unit_lines(path):
            match = re.match(r"ExecStart=@MUSE_VENV@/bin/python -m muse (.+)$", line)
            if match:
                args = match.group(1).split()
                parser.parse_args(args)           # raises SystemExit on an unknown subcommand or flag
                seen.append(" ".join(args))
    assert seen == ["chores tick", "home serve", "up --native", "queue run"]
    wrapper = (VM / "muse-catalog-check.sh").read_text(encoding="utf-8")
    match = re.search(r'-m muse (catalog check [^>]+)>', wrapper)
    parser.parse_args(match.group(1).split())
    assert "--pages" in match.group(1) and "--json" in match.group(1)
    for timer, service in (("muse-chores.timer", "muse-chores.service"), ("muse-catalog-check.timer", "muse-catalog-check.service"),
                           ("muse-backup.timer", "muse-backup.service"), ("muse-update.timer", "muse-update.service")):
        assert f"Unit={service}" in unit_lines(UNITS / timer) and "WantedBy=timers.target" in unit_lines(UNITS / timer)
    assert "OnCalendar=minutely" in unit_lines(UNITS / "muse-chores.timer")
    assert any(line.startswith("OnCalendar=Sun ") for line in unit_lines(UNITS / "muse-catalog-check.timer"))
    assert any(re.match(r"OnCalendar=\*-\*-\* \d\d:\d\d:\d\d", line) for line in unit_lines(UNITS / "muse-backup.timer"))
    # Scripts the units start exist in the tree they run from.
    for path in unit_files():
        for line in unit_lines(path):
            for script in re.findall(r"@MUSE_ROOT@/(deploy/vm/[\w.-]+)", line):
                assert (ROOT / script).is_file(), script


def test_install_services_renders_every_unit(tmp_path):
    dest = tmp_path / "units"
    result = run(["bash", str(VM / "install-services.sh"), "--user", "muse", "--root", "/opt/muse/current", "--venv", "/opt/muse/current/venv",
                  "--state", "/var/lib/muse", "--dest", str(dest)])
    assert result.returncode == 0, result.stderr
    assert {p.name for p in dest.iterdir()} == {p.name for p in unit_files()}
    for path in dest.iterdir():
        text = path.read_text(encoding="utf-8")
        assert "@" not in re.sub(r"\S+@\S+\.\S+", "", text), f"unfilled placeholder in {path.name}"
        if path.suffix == ".service":
            assert "ProtectHome=yes" in text
    proxy = (dest / "muse-proxy.service").read_text()
    assert "ExecStart=/opt/muse/current/venv/bin/python -m muse up --native" in proxy and "User=muse" in proxy
    assert "ReadWritePaths=/opt/muse /var/lib/muse /etc/systemd/system" in (dest / "muse-update.service").read_text()
    # The manual layout: a clone in the home folder, a chosen subset of units.
    manual = tmp_path / "manual"
    result = run(["bash", str(VM / "install-services.sh"), "--user", "ubuntu", "--root", "/home/ubuntu/muse-stack", "--venv", "/home/ubuntu/.muse-venv",
                  "--state", "/home/ubuntu/.muse", "--dest", str(manual), "--units", "muse-home muse-chores"])
    assert result.returncode == 0, result.stderr
    assert {p.name for p in manual.iterdir()} == {"muse-home.service", "muse-chores.service", "muse-chores.timer"}
    assert "ProtectHome=read-only" in (manual / "muse-home.service").read_text()
    assert run(["bash", str(VM / "install-services.sh"), "--user", "root", "--root", "/a", "--venv", "/b", "--state", "/c", "--dest", str(dest)]).returncode != 0
    assert run(["bash", str(VM / "install-services.sh"), "--user", "muse", "--root", "/a", "--venv", "/b", "--state", "/c", "--dest", str(dest), "--units", "nope"]).returncode != 0
    if shutil.which("systemd-analyze"):
        verify = subprocess.run(["systemd-analyze", "verify", "--recursive-errors=no", *[str(p) for p in sorted(dest.glob("*.service"))]],
                                capture_output=True, text=True, timeout=60)
        problems = [line for line in verify.stderr.splitlines()
                    if re.search(r"Unknown (key|lvalue|section)|Failed to parse|Invalid|Missing '='|not a valid", line)]
        assert not problems, problems


# ---------------------------------------------------------------- shell scripts

def all_shell_scripts() -> list[Path]:
    return sorted(VM.glob("*.sh")) + [STACK / "build-zip.sh", ROOT / "deploy" / "oracle-a1-setup.sh"]


def test_shell_scripts_parse(tmp_path):
    scripts = all_shell_scripts()
    assert len(scripts) >= 9
    for path in scripts:
        assert subprocess.run(["bash", "-n", str(path)], capture_output=True, text=True).returncode == 0, path.name
        text = path.read_text(encoding="utf-8")
        if path.name != "lib.sh":
            assert text.startswith("#!/usr/bin/env bash\n") and "set -euo pipefail" in text, path.name
            assert os.access(path, os.X_OK), f"{path.name} is not executable"
    boot = tmp_path / "first-boot.sh"
    boot.write_text(first_boot_script(SAMPLE), encoding="utf-8")
    assert subprocess.run(["bash", "-n", str(boot)], capture_output=True).returncode == 0
    for path in list(VM.glob("*.py")) + [VM / "muse-vm-status"]:
        compile(path.read_text(encoding="utf-8"), str(path), "exec")


@pytest.mark.skipif(shutil.which("shellcheck") is None, reason="shellcheck is not installed here; scripts are still checked with bash -n")
def test_shellcheck(tmp_path):
    boot = tmp_path / "first-boot.sh"
    boot.write_text(first_boot_script(SAMPLE), encoding="utf-8")
    result = subprocess.run(["shellcheck", "-x", "-S", "warning", "-P", str(ROOT), *[str(p) for p in all_shell_scripts()], str(boot)],
                            capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stdout


def test_help_of_every_script():
    for path in all_shell_scripts():
        if path.name == "lib.sh":
            continue
        result = run(["bash", str(path), "--help"])
        assert result.returncode == 0 and result.stdout.strip(), path.name
        assert not result.stdout.lstrip().startswith("set "), f"{path.name} --help prints code"
        assert "set -euo pipefail" not in result.stdout and "HERE=" not in result.stdout, path.name


def test_manual_setup_script_still_works_as_before():
    path = ROOT / "deploy" / "oracle-a1-setup.sh"
    result = run(["bash", str(path), "--help"])
    assert result.returncode == 0
    for phrase in ("--native", "--tz Asia/Taipei", "127.0.0.1:4000", "never does: open a port", "NOT TESTED on a real Oracle VM", "--services"):
        assert phrase in result.stdout, phrase
    assert run(["bash", str(path), "--bogus"]).returncode == 2
    text = path.read_text(encoding="utf-8")
    # The earlier behaviour is intact: loopback only, hash-locked native install, verification, generated key not shown.
    assert "--require-hashes -r requirements/proxy.lock -r requirements/dev.lock" in text
    assert "scripts/verify_litellm.py || fail" in text
    assert 'set_if_empty MUSE_BIND "127.0.0.1"' in text and "This script only deploys on 127.0.0.1." in text
    assert "master key: generated and stored in .env (not shown)" in text and "chmod 600 .env" in text
    assert "systemctl --user enable --now muse-dash.service" in text
    assert 'SERVICES=""' in text and 'if [ -n "$SERVICES" ]; then' in text, "the extra services are opt-in"
    assert "iptables -" not in text and "ufw " not in text and "firewall-cmd" not in text


# ------------------------------------------------------------ backup and restore

def make_home(home: Path) -> None:
    home.mkdir(parents=True)
    for name, rows in (("ledger.sqlite", 50), ("queue.sqlite", 5), ("cards.sqlite", 3)):
        conn = sqlite3.connect(home / name)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
        conn.executemany("INSERT INTO t (v) VALUES (?)", [(f"{name}-{i}",) for i in range(rows)])
        conn.commit()
        conn.close()
    (home / "apps.yaml").write_text("apps: {}\n", encoding="utf-8")
    (home / "device.key").write_text("device-signing-key", encoding="utf-8")
    (home / "queue-out").mkdir()
    (home / "queue-out" / "job-1.md").write_text("result", encoding="utf-8")
    (home / "proxy.log").write_text("log line", encoding="utf-8")
    (home / "proxy.pid").write_text("123", encoding="utf-8")
    (home / "litellm-config.yaml").write_text("rendered: true\n", encoding="utf-8")
    (home / "backup-key").mkdir()
    (home / "backup-key" / "identity.txt").write_text("AGE-SECRET-KEY-PLACEHOLDER", encoding="utf-8")
    (home / "link").symlink_to("/etc/passwd")


def rows(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        return conn.execute("SELECT count(*) FROM t").fetchone()[0]
    finally:
        conn.close()


def test_backup_stage_is_consistent_and_leaves_out_what_it_says(tmp_path):
    tool = load_script("backup_tool", VM / "backup_tool.py")
    home = tmp_path / "home"
    make_home(home)
    # A writer holds an uncommitted change while the backup runs: the copy must hold only committed rows.
    writer = sqlite3.connect(home / "ledger.sqlite")
    writer.execute("BEGIN")
    writer.execute("INSERT INTO t (v) VALUES ('uncommitted')")
    env_file = tmp_path / "muse.env"
    env_file.write_text("LITELLM_MASTER_KEY=sk-muse-test\nGEMINI_API_KEY=provider-secret\n", encoding="utf-8")

    stage = tmp_path / "stage.tar.gz"
    result = run(["bash", str(VM / "muse-backup.sh"), "--stage-only", str(stage)], env={"MUSE_HOME": str(home), "MUSE_ENV_FILE": str(env_file)})
    writer.rollback()
    writer.close()
    assert result.returncode == 0, result.stderr
    assert (stage.stat().st_mode & 0o777) == 0o600
    with tarfile.open(stage) as tar:
        names = set(tar.getnames())
        manifest = json.load(tar.extractfile("manifest.json"))
    assert names == {"manifest.json", "home/ledger.sqlite", "home/queue.sqlite", "home/cards.sqlite", "home/apps.yaml", "home/device.key", "home/queue-out/job-1.md"}
    assert manifest["includes_provider_keys"] is False, "provider keys stay out unless asked for"
    assert not any("wal" in n or "shm" in n or "backup-key" in n or n.endswith((".log", ".pid")) for n in names)
    assert {f["path"]: f["kind"] for f in manifest["files"]}["home/ledger.sqlite"] == "sqlite"

    restored = tmp_path / "restored"
    summary = tool.summary(tool.restore(stage, restored))
    assert summary["databases"] == 3 and summary["files"] == 6
    assert rows(restored / "ledger.sqlite") == 50 and rows(restored / "queue.sqlite") == 5
    assert (restored / "device.key").read_text() == "device-signing-key"
    assert (restored.stat().st_mode & 0o777) == 0o700

    # With keys asked for, they are inside the same archive, flagged in the manifest.
    with_keys = tmp_path / "with-keys.tar.gz"
    result = run(["bash", str(VM / "muse-backup.sh"), "--stage-only", str(with_keys)],
                 env={"MUSE_HOME": str(home), "MUSE_ENV_FILE": str(env_file), "MUSE_BACKUP_INCLUDE_KEYS": "1"})
    assert result.returncode == 0, result.stderr
    with tarfile.open(with_keys) as tar:
        assert "keys/muse.env" in tar.getnames() and json.load(tar.extractfile("manifest.json"))["includes_provider_keys"] is True
    assert "provider-secret" not in result.stdout + result.stderr


def test_restore_refuses_a_changed_or_hostile_archive(tmp_path):
    tool = load_script("backup_tool", VM / "backup_tool.py")
    home = tmp_path / "home"
    make_home(home)
    stage = tmp_path / "stage.tar.gz"
    tool.stage(home, stage)

    def rebuild(change) -> Path:
        out = tmp_path / f"changed-{time.monotonic_ns()}.tar.gz"
        with tarfile.open(stage) as src, tarfile.open(out, "w:gz") as dst:
            for member in src.getmembers():
                data = src.extractfile(member).read()
                member, data = change(member, data)
                if member is not None:
                    member.size = len(data)
                    dst.addfile(member, __import__("io").BytesIO(data))
        return out

    def flip(member, data):
        return (member, data + b"x") if member.name == "home/apps.yaml" else (member, data)

    def drop(member, data):
        return (None, b"") if member.name == "home/device.key" else (member, data)

    def climb(member, data):
        if member.name == "home/apps.yaml":
            member.name = "home/../../escape"
        return member, data

    for change, message in ((flip, "does not match the manifest"), (drop, "exactly the files"), (climb, "unexpected entry")):
        with pytest.raises(tool.BackupError, match=message):
            tool.restore(rebuild(change), tmp_path / f"out-{message[:4].strip()}")
    assert not (tmp_path / "escape").exists()
    assert not list(tmp_path.glob("out-*")), "a refused restore leaves nothing behind"
    with pytest.raises(tool.BackupError, match="already exists"):
        tool.restore(stage, home)


AGE = shutil.which("age") and shutil.which("age-keygen")


@pytest.mark.skipif(not AGE, reason="age is not installed here; the plain stage of the backup is tested above")
def test_encrypted_backup_and_restore_round_trip(tmp_path):
    home, state = tmp_path / "home", tmp_path / "vm"
    make_home(home)
    (home / "backup-key" / "identity.txt").unlink()          # let the script make a real key
    env = {"MUSE_HOME": str(home), "MUSE_VM_STATE": str(state), "MUSE_BACKUP_KEEP_LOCAL": "2", "MUSE_ROOT": str(tmp_path / "none")}
    for _ in range(3):
        result = run(["bash", str(VM / "muse-backup.sh")], env=env)
        assert result.returncode == 0, result.stderr
        time.sleep(1.1)                                       # names carry a one-second timestamp
    identity = home / "backup-key" / "identity.txt"
    assert identity.read_text().startswith("# created") and (identity.stat().st_mode & 0o777) == 0o600
    assert "AGE-SECRET-KEY" not in result.stdout + result.stderr, "the private key is never printed"
    archives = sorted((state / "backup" / "archives").glob("*.age"))
    assert len(archives) == 2, "only the newest two stay"
    last = json.loads((state / "backup" / "last.json").read_text())
    assert last["result"] == "local-only" and last["uploaded"] is False and last["name"] == archives[-1].name
    assert "only on this VM" in last["detail"] and "not against losing the VM" in last["readable_by"]
    assert last["sha256"] == hashlib.sha256(archives[-1].read_bytes()).hexdigest() and last["size_bytes"] == archives[-1].stat().st_size
    blob = archives[-1].read_bytes()
    assert blob.startswith(b"age-encryption.org/v1") and b"device-signing-key" not in blob and b"SQLite format" not in blob
    assert not list((state / "backup").glob(".work.*")), "the plain stage is gone"

    # Check only (no --yes) changes nothing.
    target = tmp_path / "new-home"
    check = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(identity), "--target", str(target), "--no-services"], env=env)
    assert check.returncode == 0 and "check only" in check.stdout and not target.exists()

    # A fresh machine: restore the latest into an empty place.
    done = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(identity), "--target", str(target), "--no-services", "--yes"], env=env)
    assert done.returncode == 0, done.stderr
    assert rows(target / "ledger.sqlite") == 50 and (target / "queue-out" / "job-1.md").read_text() == "result"
    assert not (target / "backup-key").exists() and not (target / "proxy.log").exists()

    # Over an existing home: the old one is moved aside, never deleted, and this machine keeps its backup key.
    (home / "apps.yaml").write_text("changed after the backup\n", encoding="utf-8")
    again = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(identity), "--target", str(home), "--no-services", "--yes"], env=env)
    assert again.returncode == 0, again.stderr
    assert (home / "apps.yaml").read_text() == "apps: {}\n" and (home / "backup-key" / "identity.txt").is_file()
    aside = list(tmp_path.glob("home.before-restore-*"))
    assert len(aside) == 1 and (aside[0] / "apps.yaml").read_text() == "changed after the backup\n"

    # The wrong key, and a damaged file, are refused with the home untouched.
    other = tmp_path / "other.txt"
    subprocess.run(["age-keygen", "-o", str(other)], capture_output=True, check=True)
    wrong = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(other), "--target", str(tmp_path / "x"), "--no-services", "--yes"], env=env)
    assert wrong.returncode != 0 and "could not decrypt" in wrong.stderr and not (tmp_path / "x").exists()
    damaged = bytearray(blob)
    damaged[-10] ^= 0xFF
    archives[-1].write_bytes(bytes(damaged))
    bad = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(identity), "--target", str(tmp_path / "y"), "--no-services", "--yes"], env=env)
    assert bad.returncode != 0 and "does not match its SHA-256" in bad.stderr and not (tmp_path / "y").exists()


def test_backup_refuses_to_run_unencrypted_or_when_off(tmp_path):
    home, state = tmp_path / "home", tmp_path / "vm"
    make_home(home)
    env = {"MUSE_HOME": str(home), "MUSE_VM_STATE": str(state)}
    off = run(["bash", str(VM / "muse-backup.sh")], env={**env, "MUSE_BACKUP": "off"})
    assert off.returncode == 0 and json.loads((state / "backup" / "last.json").read_text())["result"] == "disabled"
    bad = run(["bash", str(VM / "muse-backup.sh")], env={**env, "MUSE_BACKUP_RECIPIENT": "not-a-key"})
    if AGE:
        assert bad.returncode != 0 and "age public key" in bad.stderr
    else:
        assert bad.returncode != 0 and "age is not installed" in bad.stderr
    assert not list((state / "backup").glob("archives/*")), "nothing is written without a valid recipient"
    text = (VM / "muse-backup.sh").read_text(encoding="utf-8")
    assert text.index("age --encrypt") < text.index('oci put "$ARCHIVES/$NAME"'), "encryption comes before any upload"
    assert 'oci put "$WORK/stage' not in text and "stage.tar.gz\" \"$PREFIX" not in text


# --------------------------------------------- instance principal (a local stand-in)

class FakeOracle:
    """Metadata service, identity service and Object Storage in one loopback server. It checks every signature."""

    def __init__(self) -> None:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID

        self.serialization, self.hashes = serialization, hashes
        self.leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "ocid1.instance.oc1..fake"),
                          x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "opc-tenant:ocid1.tenancy.oc1..faketenancy")])
        now = dt.datetime.now(dt.timezone.utc)
        cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(self.leaf_key.public_key())
                .serial_number(1).not_valid_before(now - dt.timedelta(minutes=5)).not_valid_after(now + dt.timedelta(hours=1))
                .sign(self.leaf_key, hashes.SHA256()))
        self.cert_pem = cert.public_bytes(serialization.Encoding.PEM)
        self.key_pem = self.leaf_key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption())
        self.fingerprint = ":".join(f"{b:02X}" for b in cert.fingerprint(hashes.SHA1()))
        self.session_public = None
        self.objects: dict[str, bytes] = {}
        self.log: list[str] = []
        self.errors: list[str] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def _send(self, status: int, body: bytes = b"") -> None:
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self) -> None:
                body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
                try:
                    status, answer = outer.handle(self.command, self.path, self.headers, body)
                except AssertionError as exc:
                    outer.errors.append(str(exc))
                    status, answer = 401, str(exc).encode()
                self._send(status, answer)

            do_GET = do_POST = do_PUT = do_DELETE = _handle

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def env(self) -> dict[str, str]:
        return {"MUSE_OCI_IMDS": f"{self.url}/opc/v2", "MUSE_OCI_AUTH_ENDPOINT": self.url, "MUSE_OCI_OS_ENDPOINT": self.url,
                "MUSE_BACKUP_NAMESPACE": "examplens", "MUSE_BACKUP_BUCKET": "muse-backups-abc123"}

    def _verify(self, public_key, method: str, path: str, headers, expected_key_id: str, expected_names: list[str]) -> None:
        from cryptography.hazmat.primitives.asymmetric import padding

        fields = dict(re.findall(r'(\w+)="([^"]*)"', headers["Authorization"]))
        assert headers["Authorization"].startswith("Signature ") and fields["version"] == "1" and fields["algorithm"] == "rsa-sha256"
        assert fields["keyId"] == expected_key_id, f"unexpected keyId {fields['keyId'][:40]}"
        assert fields["headers"].split() == expected_names, fields["headers"]
        lines = [f"{n}: {method.lower()} {path}" if n == "(request-target)" else f"{n}: {headers[n]}" for n in expected_names]
        public_key.verify(base64.b64decode(fields["signature"]), "\n".join(lines).encode(), padding.PKCS1v15(), self.hashes.SHA256())

    def handle(self, method: str, path: str, headers, body: bytes) -> tuple[int, bytes]:
        self.log.append(f"{method} {path.split('?')[0]}")
        if path.startswith("/opc/v2/"):
            assert headers.get("Authorization") == "Bearer Oracle", "the metadata service needs its header"
            if path == "/opc/v2/instance/":
                return 200, json.dumps({"canonicalRegionName": "test-region-1", "regionInfo": {"realmDomainComponent": "example.invalid"}}).encode()
            return 200, {"/opc/v2/identity/cert.pem": self.cert_pem, "/opc/v2/identity/key.pem": self.key_pem,
                         "/opc/v2/identity/intermediate.pem": self.cert_pem}[path]
        if path == "/v1/x509":
            assert headers["x-content-sha256"] == base64.b64encode(hashlib.sha256(body).digest()).decode()
            self._verify(self.leaf_key.public_key(), method, path, headers, f"ocid1.tenancy.oc1..faketenancy/fed-x509/{self.fingerprint}",
                         ["date", "(request-target)", "content-length", "content-type", "x-content-sha256"])
            doc = json.loads(body)
            assert "-----" not in doc["certificate"] and "\n" not in doc["certificate"] and doc["intermediateCertificates"]
            pem = "-----BEGIN PUBLIC KEY-----\n" + doc["publicKey"] + "\n-----END PUBLIC KEY-----\n"
            self.session_public = self.serialization.load_pem_public_key(pem.encode())
            return 200, json.dumps({"token": "fake.session.token"}).encode()
        prefix = "/n/examplens/b/muse-backups-abc123/o"
        assert path.startswith(prefix), f"outside the bucket: {path}"
        assert self.session_public is not None, "no login before the first object call"
        self._verify(self.session_public, method, path, headers, "ST$fake.session.token", ["date", "(request-target)", "host"])
        from urllib.parse import parse_qs, unquote, urlsplit

        parts = urlsplit(path)
        if parts.path == prefix:
            wanted = parse_qs(parts.query).get("prefix", [""])[0]
            items = [{"name": n, "size": len(v), "timeCreated": "2026-11-13T00:00:00Z"} for n, v in sorted(self.objects.items()) if n.startswith(wanted)]
            return 200, json.dumps({"objects": items}).encode()
        name = unquote(parts.path[len(prefix) + 1:])
        if method == "PUT":
            self.objects[name] = body
            return 200, b""
        if method == "GET":
            return (200, self.objects[name]) if name in self.objects else (404, b"{}")
        if method == "DELETE":
            return (204, b"") if self.objects.pop(name, None) is not None else (404, b"{}")
        return 405, b""

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def oracle():
    pytest.importorskip("cryptography")
    fake = FakeOracle()
    yield fake
    fake.close()


def test_instance_principal_signs_every_request(oracle, tmp_path):
    source = tmp_path / "file.bin"
    source.write_bytes(b"ciphertext " * 100)
    tool = [sys.executable, str(VM / "oci_object.py")]
    assert run([*tool, "put", str(source), "backups/a b.age"], env=oracle.env()).returncode == 0
    assert oracle.objects == {"backups/a b.age": source.read_bytes()} and not oracle.errors
    listed = run([*tool, "list", "backups/"], env=oracle.env())
    assert [json.loads(line)["name"] for line in listed.stdout.splitlines()] == ["backups/a b.age"]
    target = tmp_path / "back.bin"
    assert run([*tool, "get", "backups/a b.age", str(target)], env=oracle.env()).returncode == 0
    assert target.read_bytes() == source.read_bytes() and (target.stat().st_mode & 0o777) == 0o600
    assert run([*tool, "delete", "backups/a b.age"], env=oracle.env()).returncode == 0 and not oracle.objects
    missing = run([*tool, "get", "backups/none", str(tmp_path / "n")], env=oracle.env())
    assert missing.returncode == 1 and "HTTP 404" in missing.stderr and "fake.session.token" not in missing.stderr
    assert not oracle.errors, oracle.errors
    assert "POST /v1/x509" in oracle.log and "GET /opc/v2/identity/key.pem" in oracle.log
    # Plain http is refused except for the metadata address and this loopback stand-in.
    refused = run([*tool, "list"], env={**oracle.env(), "MUSE_OCI_OS_ENDPOINT": "http://objectstorage.example.invalid"})
    assert refused.returncode == 1 and "not https" in refused.stderr
    assert run([*tool, "list"], env={**oracle.env(), "MUSE_BACKUP_BUCKET": ""}).returncode == 1
    source_text = (VM / "oci_object.py").read_text(encoding="utf-8")
    login = source_text.split("def login", 1)[1].split("def request", 1)[0]
    assert not re.search(r"(?<![\w.])open\(|write_text|write_bytes", login), "instance keys are never written to disk"


@pytest.mark.skipif(not AGE, reason="age is not installed here")
def test_backup_uploads_encrypted_files_and_keeps_the_newest(oracle, tmp_path):
    home, state = tmp_path / "home", tmp_path / "vm"
    make_home(home)
    (home / "backup-key" / "identity.txt").unlink()
    env = {**oracle.env(), "MUSE_HOME": str(home), "MUSE_VM_STATE": str(state), "MUSE_BACKUP_KEEP": "2", "MUSE_ROOT": str(tmp_path / "none")}
    for _ in range(3):
        result = run(["bash", str(VM / "muse-backup.sh")], env=env)
        assert result.returncode == 0, result.stderr
        time.sleep(1.1)
    assert not oracle.errors, oracle.errors
    ages = sorted(n for n in oracle.objects if n.endswith(".age"))
    assert len(ages) == 2 and all(n.startswith("backups/muse-backup-") for n in ages), "the bucket keeps the newest two"
    assert sorted(oracle.objects) == sorted(ages + [n + ".sha256" for n in ages])
    for name in ages:
        assert oracle.objects[name].startswith(b"age-encryption.org/v1"), "only ciphertext leaves the machine"
        assert hashlib.sha256(oracle.objects[name]).hexdigest() == oracle.objects[name + ".sha256"].decode().split()[0]
    last = json.loads((state / "backup" / "last.json").read_text())
    assert last["result"] == "ok" and last["uploaded"] is True and last["bucket"] == "muse-backups-abc123" and last["pruned_remote"] == 1

    # A new machine with only the bucket and the key: fetch the latest, verify, decrypt, restore.
    fresh_state, target = tmp_path / "fresh-vm", tmp_path / "fresh-home"
    restore = run(["bash", str(VM / "muse-restore.sh"), "--identity", str(home / "backup-key" / "identity.txt"), "--target", str(target), "--no-services", "--yes"],
                  env={**oracle.env(), "MUSE_VM_STATE": str(fresh_state)})
    assert restore.returncode == 0, restore.stderr
    assert rows(target / "ledger.sqlite") == 50 and "fetched " + ages[-1] in restore.stdout

    # When the upload fails the backup is kept locally and the result says so.
    oracle.close()
    failed = run(["bash", str(VM / "muse-backup.sh")], env=env)
    assert failed.returncode != 0
    last = json.loads((state / "backup" / "last.json").read_text())
    assert last["result"] == "upload-failed" and last["uploaded"] is False and (state / "backup" / "archives" / last["name"]).is_file()


# ---------------------------------------------------------------------- updates

def signed_manifest(doc: dict) -> tuple[bytes, bytes, str]:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    key = Ed25519PrivateKey.generate()
    manifest = json.dumps(doc).encode()
    public = key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    return manifest, base64.b64encode(key.sign(manifest)), base64.b64encode(public).decode()


def test_update_manifest_must_be_signed_fixed_and_newer():
    pytest.importorskip("cryptography")
    verify = load_script("update_verify", VM / "update_verify.py")
    good = {"channel": "stable", "version": "0.3.0", "url": "https://releases.example/muse-0.3.0.tar.gz", "sha256": SHA}
    manifest, signature, public = signed_manifest(good)
    assert verify.decide(manifest, signature, public, "0.2.0") == f"newer 0.3.0 {good['url']} {SHA}"
    assert verify.decide(manifest, signature, public, "0.3.0") == "same 0.3.0"
    assert verify.decide(manifest, signature, public, "0.10.0") == "older 0.3.0", "versions compare as numbers; a downgrade is reported, not installed"
    with pytest.raises(verify.Refused, match="no public key"):
        verify.decide(manifest, signature, "", "0.2.0")
    with pytest.raises(verify.Refused, match="signature does not match"):
        verify.decide(manifest.replace(b"0.3.0", b"0.3.1"), signature, public, "0.2.0")
    _, _, other_public = signed_manifest(good)
    with pytest.raises(verify.Refused, match="signature does not match"):
        verify.decide(manifest, signature, other_public, "0.2.0")
    for change, message in (({"version": "latest"}, "not a fixed version"), ({"version": "main"}, "not a fixed version"),
                            ({"url": "http://releases.example/m.tar.gz"}, "not https"), ({"sha256": "abc"}, "not a SHA-256"),
                            ({"channel": "beta"}, "channel")):
        manifest, signature, public = signed_manifest({**good, **change})
        with pytest.raises(verify.Refused, match=message):
            verify.decide(manifest, signature, public, "0.2.0")


def test_updates_are_off_by_default_and_never_unverified(tmp_path):
    state = tmp_path / "vm"
    env = {"MUSE_VM_STATE": str(state), "MUSE_BASE": str(tmp_path / "opt"), "MUSE_USER": "nobody-such-user"}
    off = run(["bash", str(VM / "muse-update.sh")], env=env)
    assert off.returncode == 0 and "updates are off" in off.stdout
    assert json.loads((state / "update" / "last.json").read_text())["result"] == "disabled"
    # One of the two settings alone is still off.
    half = run(["bash", str(VM / "muse-update.sh")], env={**env, "MUSE_UPDATE_MANIFEST_URL": "https://updates.example/stable.json"})
    assert half.returncode == 0 and json.loads((state / "update" / "last.json").read_text())["result"] == "disabled"
    plain = run(["bash", str(VM / "muse-update.sh")], env={**env, "MUSE_UPDATE_MANIFEST_URL": "http://updates.example/stable.json", "MUSE_UPDATE_PUBKEY": "A" * 44})
    assert plain.returncode != 0 and json.loads((state / "update" / "last.json").read_text())["result"] == "refused"
    text = (VM / "muse-update.sh").read_text(encoding="utf-8")
    order = [text.index(marker) for marker in ('update_verify.py"', 'fetch_https "$URL"', 'sha256_of "$WORK/release.tar.gz"', "unpack_release", "build_release_env",
                                               'switch_release "$BASE" "$VERSION"', 'switch_release "$BASE" "$CURRENT"')]
    assert order == sorted(order), "verify the manifest, then the archive, then build and test, then switch; roll back last"
    lib = (VM / "lib.sh").read_text(encoding="utf-8")
    assert "--require-hashes -r \"$dir/requirements/proxy.lock\"" in lib and "scripts/verify_litellm.py" in lib
    assert "pip install --upgrade" not in lib and "git clone" not in lib + text
    conf = yaml.safe_load(render_template(SAMPLE))["write_files"][0]["content"]
    assert "MUSE_UPDATE_MANIFEST_URL=\n" in conf and "MUSE_UPDATE_PUBKEY=\n" in conf, "the stack ships with updates off"


# ------------------------------------------------------- monitor, idle guard, status

def test_idle_guard_uses_the_processor_only_and_backs_off():
    monitor = load_script("muse_vm_monitor", VM / "muse-vm-monitor.py")
    # The setting: auto by default, which is on for an Always Free account and off for a paid one.
    on, target, why = monitor.guard_settings({})
    assert (on, target) == (True, 25.0) and "Always Free" in why
    assert monitor.guard_settings({"MUSE_ACCOUNT_TYPE": "always_free"})[0] is True
    assert monitor.guard_settings({"MUSE_ACCOUNT_TYPE": "paid"})[:2] == (False, 25.0)
    assert "paid" in monitor.guard_settings({"MUSE_ACCOUNT_TYPE": "paid"})[2]
    assert monitor.guard_settings({"MUSE_ACCOUNT_TYPE": "paid", "MUSE_IDLE_GUARD": "on"})[0] is True
    assert monitor.guard_settings({"MUSE_ACCOUNT_TYPE": "always_free", "MUSE_IDLE_GUARD": "off"})[0] is False
    assert monitor.guard_settings({"MUSE_IDLE_GUARD": "auto", "MUSE_ACCOUNT_TYPE": "nonsense"})[0] is True
    assert monitor.guard_settings({"MUSE_IDLE_GUARD_TARGET_PCT": "5"})[1] == 20.5
    assert monitor.guard_settings({"MUSE_IDLE_GUARD_TARGET_PCT": "90"})[1] == 40.0
    assert monitor.ORACLE_IDLE_PCT == 20.0
    # One minute in ten: twice what a 95th percentile needs (just over 5% of minutes).
    assert monitor.GUARD_EVERY_MIN == 10 and 1 / monitor.GUARD_EVERY_MIN >= 2 * 0.05
    assert not monitor.guard_due(0) and not monitor.guard_due(8) and monitor.guard_due(9)
    # Half a core on 2 OCPUs is 25% of the machine; never more than one thread can run.
    assert monitor.guard_duty(25.0, 2) == 0.5 and monitor.guard_duty(25.0, 1) == 0.25
    assert monitor.guard_duty(40.0, 8) == monitor.MAX_DUTY <= 1.0
    # Other work is measured without the guard's own share, and stops a guard minute above BACK_OFF_PCT.
    assert monitor.others_pct(50, 100, 0.5, 2, 1.0) == 25.0
    assert monitor.others_pct(25, 100, 0.5, 2, 1.0) == 0.0
    assert monitor.others_pct(0, 0, 0.0, 2, 1.0) == 0.0
    assert monitor.BACK_OFF_PCT < 20.0
    # No memory reservation of any kind, and nothing that could talk to a network.
    text = (VM / "muse-vm-monitor.py").read_text(encoding="utf-8")
    assert "mmap" not in text and "Reservation" not in text and "socket" not in text and "urllib" not in text
    # A guard minute really runs and stops when asked.
    guard = monitor.Guard(25.0, 2)
    guard.start(5.0)
    time.sleep(0.3)
    guard.halt()
    assert not guard.running() and guard.busy_s > 0
    # The daily line says what it did and why, in one line.
    stats = {"guard_minutes": 140, "busy_minutes": 12, "backed_off": 3}
    line = monitor.daily_line("2026-11-14", True, why, 25.0, stats)
    assert "\n" not in line and "140 minutes" in line and "Always Free" in line and "backed off for real work 3 times" in line
    assert "never holds memory or sends traffic" in line
    off = monitor.daily_line("2026-11-14", False, "MUSE_IDLE_GUARD=off", 25.0, stats)
    assert "\n" not in off and "off (MUSE_IDLE_GUARD=off)" in off and "did nothing" in off
    # Readers.
    meminfo = monitor.parse_meminfo("MemTotal:       12000000 kB\nMemFree:  100 kB\nMemAvailable:    9000000 kB\n")
    assert round(monitor.memory_used_pct(meminfo), 1) == 25.0
    assert monitor.parse_cpu("cpu  100 0 100 700 100 0 0 0 0 0\ncpu0 1 2 3 4\n") == (200, 1000)
    assert monitor.parse_net("h1\nh2\n    lo: 500 0 0 0 0 0 0 0 500 0\n  ens3: 1000 0 0 0 0 0 0 0 2000 0 0 0 0 0 0 0\n") == 3000
    unit = "\n".join(unit_lines(UNITS / "muse-vm-monitor.service"))
    assert "MemoryMax=128M" in unit and "OOMScoreAdjust=1000" in unit, "small, and first to go if memory runs short"
    assert "CPUQuota=100%" in unit and "CPUWeight=1" in unit and "Nice=19" in unit, "at most one core, always behind real work"
    assert "IPAddressDeny=any" in unit and "RestrictAddressFamilies=AF_UNIX" in unit


def test_idle_guard_default_follows_the_account_type(tf):
    variables = {b.labels[0]: b for b in _top(tf, "variable")}
    assert variables["account_type"].attrs["default"] == '"always_free"'
    assert variables["idle_guard"].attrs["default"] == '"auto"' and variables["idle_guard"].attrs["type"] == "string"
    locals_ = {k: v for b in _top(tf, "locals") for k, v in b.attrs.items()}
    assert re.sub(r"\s+", " ", locals_["idle_guard_on"]) == 'var.idle_guard == "auto" ? var.account_type == "always_free" : var.idle_guard == "on"'
    schema = yaml.safe_load((STACK / "schema.yaml").read_text(encoding="utf-8"))
    assert schema["variables"]["idle_guard"]["enum"] == ["auto", "on", "off"]
    assert schema["variables"]["account_type"]["enum"] == ["always_free", "paid"]
    conf = yaml.safe_load(render_template(SAMPLE))["write_files"][0]["content"]
    assert "MUSE_ACCOUNT_TYPE=always_free\n" in conf and "MUSE_IDLE_GUARD=on\n" in conf
    paid = yaml.safe_load(render_template({**SAMPLE, "account_type": "paid", "idle_guard": "off"}))["write_files"][0]["content"]
    assert "MUSE_ACCOUNT_TYPE=paid\n" in paid and "MUSE_IDLE_GUARD=off\n" in paid
    setup = (ROOT / "deploy" / "oracle-a1-setup.sh").read_text(encoding="utf-8")
    assert "MUSE_ACCOUNT_TYPE=always_free\nMUSE_IDLE_GUARD=auto\n" in setup


def test_monitor_samples_and_status_json(tmp_path):
    state = tmp_path / "vm"
    once = run([sys.executable, str(VM / "muse-vm-monitor.py"), "--once"], env={"MUSE_VM_STATE": str(state), "MUSE_IDLE_GUARD": "off"})
    assert once.returncode == 0, once.stderr
    files = list((state / "samples").glob("*.jsonl"))
    assert len(files) == 1
    sample = json.loads(files[0].read_text().splitlines()[0])
    assert set(sample) == {"t", "cpu", "mem", "net_bps", "guard_s"} and sample["guard_s"] == 0 and 0 <= sample["mem"] <= 100
    guard = json.loads((state / "idle-guard.json").read_text())
    assert guard["enabled"] is False and guard["oracle_idle_threshold_pct"] == 20.0 and "CpuUtilization" in guard["metric"]
    assert guard["why"] == "MUSE_IDLE_GUARD=off" and "never memory or network" in guard["uses"]

    status = load_script("muse_vm_status", VM / "muse-vm-status")
    now = 1794700800.0                                   # 2026-11-15 00:00 UTC
    month_start = 1793491200                             # 2026-11-01 00:00 UTC
    assert time.gmtime(month_start)[:3] == (2026, 11, 1) and time.gmtime(now)[:3] == (2026, 11, 15)
    samples = state / "samples"
    shutil.rmtree(samples)
    samples.mkdir()
    with open(samples / "2026-11.jsonl", "w", encoding="utf-8") as handle:
        for minute in range(14 * 24 * 60):               # running since the start of the month
            handle.write(json.dumps({"t": month_start + minute * 60, "cpu": 3.0 if minute % 100 else 60.0, "mem": 26.0, "net_bps": 20000, "guard_s": 0}) + "\n")
    (state / "backup").mkdir(exist_ok=True)
    (state / "backup" / "last.json").write_text(json.dumps({"result": "ok", "finished_at": "2026-11-14T18:10:00Z", "size_bytes": 1234}), encoding="utf-8")
    (tmp_path / "setup").mkdir()
    (tmp_path / "setup" / "DONE").write_text("version=0.2.0\n")
    doc = status.status({"MUSE_VM_STATE": str(state), "MUSE_VM_CONF": "/nonexistent", "MUSE_SETUP_DIR": str(tmp_path / "setup"), "MUSE_OCPUS": "2", "MUSE_MEMORY_GB": "12"}, now=now)
    json.dumps(doc)
    week = doc["last_7_days"]
    assert week["samples"] == 7 * 24 * 60 and week["coverage_pct"] == 100.0
    assert week["cpu_pct"]["p95"] == 3.0 and week["cpu_pct"]["max"] == 60.0 and week["memory_pct"]["p95"] == 26.0
    rule = week["oracle_idle_rule"]
    assert rule["below_threshold"] == {"cpu": True, "network": True, "memory": False} and rule["would_count_as_idle"] is False
    allowance = doc["allowance"]
    assert allowance["ocpu_hours_free"] == 1500 and allowance["gb_hours_free"] == 9000 and allowance["month"] == "2026-11"
    assert allowance["ocpu_hours_used"] == 14 * 24 * 2 and allowance["gb_hours_used"] == 14 * 24 * 12
    # A whole 30-day month at 2 OCPU / 12 GB: 1,440 and 8,640, inside 1,500 and 9,000 (a 31-day month: 1,488 and 8,928).
    assert allowance["ocpu_hours_projected"] == 1440 and allowance["gb_hours_projected"] == 8640 and allowance["within_free_allowance"] is True
    assert 31 * 24 * 2 <= 1500 and 31 * 24 * 12 <= 9000
    assert doc["backup"]["result"] == "ok" and doc["setup"] == {"state": "done"} and doc["update"] is None
    assert doc["shape"] == {"ocpus": 2.0, "memory_gb": 12.0} and doc["running_since"]
    # With the guard off and everything quiet, the status says the instance would count as idle.
    with open(samples / "2026-11.jsonl", "w", encoding="utf-8") as handle:
        for minute in range(7 * 24 * 60):
            handle.write(json.dumps({"t": int(now) - (minute + 1) * 60, "cpu": 2.0, "mem": 9.0, "net_bps": 1000, "guard_s": 0}) + "\n")
    assert status.status({"MUSE_VM_STATE": str(state), "MUSE_VM_CONF": "/nonexistent"}, now=now)["last_7_days"]["oracle_idle_rule"]["would_count_as_idle"] is True
    assert status.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 95) == 10 and status.percentile([], 95) is None
    cli = run([sys.executable, str(VM / "muse-vm-status")], env={"MUSE_VM_STATE": str(state)})
    assert cli.returncode == 0 and set(json.loads(cli.stdout)) >= {"allowance", "backup", "update", "last_7_days", "idle_guard", "running_since", "os_updates"}


def test_catalog_check_wrapper_keeps_the_result(tmp_path):
    state = tmp_path / "vm"
    fake = tmp_path / "fake-python"
    fake.write_text("#!/bin/bash\n# stands in for `python -m muse catalog check --pages --json`\n"
                    "if [ \"$1\" = -m ]; then echo '{\"stale\": []}'; exit 3; fi\nexec " + sys.executable + " \"$@\"\n", encoding="utf-8")
    fake.chmod(0o755)
    result = run(["bash", str(VM / "muse-catalog-check.sh")], env={"MUSE_VM_STATE": str(state), "MUSE_PYTHON": str(fake)})
    assert result.returncode == 0, result.stderr
    assert json.loads((state / "catalog-check.json").read_text()) == {"stale": []}
    status = json.loads((state / "catalog-check.status.json").read_text())
    assert status["exit_status"] == 3 and status["checked_at"].endswith("Z")


# ------------------------------------------------------------------- the stack zip

def build_zip(tmp_path: Path, out: str, *extra: str) -> subprocess.CompletedProcess:
    args = ["--version", "0.2.0", "--release-url", "https://releases.example/muse-0.2.0.tar.gz", "--release-sha256", SHA]
    merged = list(args)
    for flag, value in zip(extra[0::2], extra[1::2]):
        merged[merged.index(flag) + 1] = value
    return run(["bash", str(STACK / "build-zip.sh"), *merged, "--out", str(tmp_path / out)], cwd=tmp_path)


def test_build_zip_is_deterministic_and_pins_the_release(tmp_path):
    first = build_zip(tmp_path, "a.zip")
    assert first.returncode == 0, first.stderr
    time.sleep(1.1)
    second = build_zip(tmp_path, "b.zip")
    digest = hashlib.sha256((tmp_path / "a.zip").read_bytes()).hexdigest()
    assert digest == hashlib.sha256((tmp_path / "b.zip").read_bytes()).hexdigest(), "two runs must give the same bytes"
    assert f"sha256:    {digest}" in first.stdout and second.stdout == first.stdout.replace("a.zip", "b.zip")
    with zipfile.ZipFile(tmp_path / "a.zip") as archive:
        names = archive.namelist()
        assert names == sorted(names) and "/" not in "".join(names), "Terraform files sit at the root of the zip"
        assert set(names) == {p.name for p in STACK.glob("*.tf")} | {"schema.yaml", "cloud-init.yaml.tftpl"}
        assert all(info.date_time == (2020, 1, 1, 0, 0, 0) for info in archive.infolist())
        assert not any(n.endswith((".tfstate", ".md", ".sh")) or n.startswith(".terraform") for n in names)
        pin = parse_hcl(archive.read("release_pin.tf").decode(), "release_pin.tf").blocks[0].attrs["pinned_release"]
        for name in names:
            if name.endswith(".tf"):
                parse_hcl(archive.read(name).decode(), name)
            assert name == "release_pin.tf" or archive.read(name) == (STACK / name).read_bytes(), name
    assert '"0.2.0"' in pin and f'"{SHA}"' in pin and '"https://releases.example/muse-0.2.0.tar.gz"' in pin
    # A different release gives a different zip.
    assert build_zip(tmp_path, "c.zip", "--release-sha256", "b" * 64).returncode == 0
    assert hashlib.sha256((tmp_path / "c.zip").read_bytes()).hexdigest() != digest
    # The repository copy is never modified by a build.
    assert '= ""' in (STACK / "release_pin.tf").read_text(encoding="utf-8")


@pytest.mark.parametrize("flag,value", [("--version", "main"), ("--version", "latest"), ("--version", "1.2"), ("--release-sha256", "abc"),
                                        ("--release-sha256", "A" * 64), ("--release-url", "http://releases.example/m.tar.gz"),
                                        ("--release-url", "https://releases.example/m\".tar.gz")])
def test_build_zip_refuses_anything_unpinned(tmp_path, flag, value):
    result = build_zip(tmp_path, "x.zip", flag, value)
    assert result.returncode == 1 and "ERROR" in result.stderr and not (tmp_path / "x.zip").exists()


def test_release_tarball_is_reproducible(tmp_path):
    if shutil.which("git") is None or subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True).returncode != 0:
        pytest.skip("not a git checkout")
    outs = []
    for name in ("one", "two"):
        result = run(["bash", str(VM / "build-release.sh"), "--version", "0.2.0", "--ref", "HEAD", "--out", str(tmp_path / name)], cwd=ROOT)
        assert result.returncode == 0, result.stderr
        outs.append(hashlib.sha256((tmp_path / name / "muse-0.2.0.tar.gz").read_bytes()).hexdigest())
        assert f"sha256:   {outs[-1]}" in result.stdout
    assert outs[0] == outs[1]
    with tarfile.open(tmp_path / "one" / "muse-0.2.0.tar.gz") as tar:
        names = tar.getnames()
    assert {n.split("/")[0] for n in names} == {"muse-0.2.0"}
    assert not any(Path(n).name == ".env" for n in names)
    assert run(["bash", str(VM / "build-release.sh"), "--version", "main", "--out", str(tmp_path / "x")], cwd=ROOT).returncode == 1


# ------------------------------------------------------------- supply chain, docs

def test_supply_chain_rules_still_pass():
    result = subprocess.run([sys.executable, str(ROOT / "scripts" / "validate_config.py")], capture_output=True, text=True, timeout=300, cwd=ROOT)
    assert result.returncode == 0, result.stdout[-2000:] + result.stderr[-2000:]
    assert result.stdout.strip().splitlines()[-1] == "config ok"


def test_no_second_copy_of_the_litellm_pin():
    lock = (ROOT / "requirements" / "proxy.lock").read_text(encoding="utf-8")
    version = re.search(r"^litellm==(\S+)", lock, re.MULTILINE).group(1)
    digest = re.search(r"litellm:v[\d.]+@sha256:([0-9a-f]{64})", (ROOT / "docker-compose.yml").read_text(encoding="utf-8")).group(1)
    for path in [*STACK.iterdir(), *VM.rglob("*")]:
        if not path.is_file() or "__pycache__" in path.parts:
            continue
        text = path.read_text(encoding="utf-8")
        assert version not in text and digest not in text, f"{path.name} repeats the LiteLLM pin; it must come from requirements/proxy.lock"
        assert not re.search(r"ghcr\.io/berriai|litellm/litellm|docker (run|pull|compose)", text), f"{path.name}: the VM stack runs natively, no image"
        assert not re.search(r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba)?sh", text), f"{path.name} pipes a download into a shell"


def test_readme_states_the_facts_and_its_limits():
    readme = (STACK / "README.md").read_text(encoding="utf-8")
    for phrase in ("2 OCPU", "12 GB", "1,500 OCPU", "9,000 GB", "Not tested yet", "https://cloud.oracle.com/resourcemanager/stacks/create?zipUrl=",
                   "deploy-to-oracle-cloud.svg", "ss -tlnH", "Idle guard", "95th percentile", "docs.oracle.com", "could not confirm",
                   "CpuUtilization", "How to delete everything", "muse-restore.sh", "<RELEASE-ZIP-URL>"):
        assert phrase in readme, phrase
    assert "4 OCPU" not in readme.replace("not 4 OCPU", "")
    cloud = (ROOT / "deploy" / "oracle-cloud.md").read_text(encoding="utf-8")
    assert "oracle-stack/README.md" in cloud and "2 OCPU and 12 GB" in cloud
    assert "up to 4 OCPU and 24 GB" not in cloud
