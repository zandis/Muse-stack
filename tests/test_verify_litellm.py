"""scripts/verify_litellm.py with fakes: a made-up site-packages and a fake registry. No network."""

from __future__ import annotations

import base64
import hashlib
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import verify_litellm as v  # noqa: E402

BAD = "sha256:1bbcb1443bc962e45f5aad53fe3d2c30600e0379715fb6c664cdd3ac126c339e"
AMD = "sha256:" + "a" * 64
ARM = "sha256:" + "b" * 64


# ---------------------------------------------------------------- fakes

def record_hash(data: bytes) -> str:
    return "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b"=").decode()


def install(site: Path, version: str = "1.108.5", name: str = "litellm", record: bool = True) -> Path:
    """A minimal installed distribution, laid out the way pip leaves one."""
    site.mkdir(parents=True, exist_ok=True)
    pkg = site / name
    pkg.mkdir(exist_ok=True)
    files = {f"{name}/__init__.py": b"VERSION = 1\n", f"{name}/main.py": b"def run():\n    return 42\n"}
    for rel, data in files.items():
        (site / rel).write_bytes(data)
    info = site / f"{name}-{version}.dist-info"
    info.mkdir()
    metadata = f"Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n".encode()
    (info / "METADATA").write_bytes(metadata)
    if record:
        lines = [f"{rel},{record_hash(data)},{len(data)}" for rel, data in files.items()]
        lines.append(f"{info.name}/METADATA,{record_hash(metadata)},{len(metadata)}")
        lines.append(f"{info.name}/RECORD,,")
        lines.append(f"{name}/__pycache__/main.cpython-312.pyc,,")  # written at install time: no hash
        (info / "RECORD").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return site


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    """A stand-in repo root: a lock file and a compose file."""
    root = tmp_path / "repo"
    (root / "requirements").mkdir(parents=True)
    (root / "requirements" / "proxy.lock").write_text(
        "# header\naiohttp==3.14.5 \\\n    --hash=sha256:" + "1" * 64 + "\nlitellm==1.108.5 \\\n    --hash=sha256:" + "2" * 64 + "\n",
        encoding="utf-8")
    return root


def index(*, platforms=(("linux", "amd64", AMD), ("linux", "arm64", ARM))) -> bytes:
    manifests = []
    for os_name, arch, digest in platforms:
        manifests.append({"digest": digest, "platform": {"os": os_name, "architecture": arch}})
        manifests.append({"digest": "sha256:" + "c" * 64, "platform": {"os": "unknown", "architecture": "unknown"},
                          "annotations": {"vnd.docker.reference.type": "attestation-manifest"}})
    return json.dumps({"schemaVersion": 2, "mediaType": "application/vnd.oci.image.index.v1+json", "manifests": manifests}).encode()


def digest_of(body: bytes) -> str:
    return "sha256:" + hashlib.sha256(body).hexdigest()


class FakeRegistry:
    """Answers the token request and one manifest request; records what was asked."""

    def __init__(self, body: bytes = b"", *, token_status: int = 200, manifest_status: int = 200, stated: str | None = None,
                 raises: bool = False) -> None:
        self.body, self.token_status, self.manifest_status, self.stated, self.raises = body, token_status, manifest_status, stated, raises
        self.calls: list[tuple[str, dict, str]] = []

    def __call__(self, url, headers, method="GET"):
        self.calls.append((url, dict(headers), method))
        if self.raises:
            raise v.CannotCheck("URLError while asking ghcr.io")
        if "/token" in url:
            return self.token_status, {}, json.dumps({"token": "anon"}).encode()
        headers_out = {"docker-content-digest": self.stated or digest_of(self.body)}
        return self.manifest_status, headers_out, self.body


def compose(root: Path, image: str, name: str = "docker-compose.yml") -> None:
    (root / name).write_text(f"services:\n  litellm:\n    image: {image}\n    restart: unless-stopped\n", encoding="utf-8")


def levels(findings) -> list[str]:
    return [f.level for f in findings]


def text(findings) -> str:
    return "\n".join(f"[{f.level}] {f.message}" for f in findings)


# ---------------------------------------------------------------- image references

@pytest.mark.parametrize("raw, registry, repository, tag, digest", [
    ("ghcr.io/berriai/litellm:v1.108.5", "ghcr.io", "berriai/litellm", "v1.108.5", None),
    (f"ghcr.io/berriai/litellm:v1.108.5@{AMD}", "ghcr.io", "berriai/litellm", "v1.108.5", AMD),
    (f"ghcr.io/berriai/litellm@{AMD}", "ghcr.io", "berriai/litellm", None, AMD),
    ("litellm/litellm:latest", "docker.io", "litellm/litellm", "latest", None),
    ("postgres:16-alpine", "docker.io", "library/postgres", "16-alpine", None),
    ("localhost:5000/x/litellm", "localhost:5000", "x/litellm", None, None),
])
def test_parse_image(raw, registry, repository, tag, digest):
    ref = v.parse_image(raw)
    assert (ref.registry, ref.repository, ref.tag, ref.digest) == (registry, repository, tag, digest)


def test_compose_images_reads_every_image_line_and_ignores_comments():
    found = v.compose_images('services:\n  db:\n    image: postgres:16-alpine\n  # image: old/litellm:latest\n'
                             f'  litellm:\n    image: "ghcr.io/berriai/litellm:v1.108.5@{AMD}"  # pinned\n')
    assert [r.raw for r in found] == ["postgres:16-alpine", f"ghcr.io/berriai/litellm:v1.108.5@{AMD}"]
    assert [r.is_litellm for r in found] == [False, True]


@pytest.mark.parametrize("raw, word", [
    ("ghcr.io/berriai/litellm:v1.108.5", "no @sha256"),
    ("ghcr.io/berriai/litellm", "no @sha256"),
    ("ghcr.io/berriai/litellm:latest", "floating"),
    (f"ghcr.io/berriai/litellm:main-latest@{AMD}", "floating"),
    (f"ghcr.io/berriai/litellm:main-stable@{AMD}", "floating"),
    (f"ghcr.io/berriai/litellm:main@{AMD}", "floating"),
    ("ghcr.io/berriai/litellm:v1.108.5@sha256:abc", "not a full sha256"),
    (f"ghcr.io/berriai/litellm:v1.108.5@{BAD}", "compromised"),
    ("ghcr.io/berriai/litellm-database:v1.108.5@sha256:c00f9d92c427" + "0" * 52, "compromised"),
    (f"ghcr.io/berriai/litellm:v1.107.0@{AMD}", "compromised release"),
])
def test_image_pin_problems(raw, word):
    assert any(word in p for p in v.image_pin_problems(v.parse_image(raw)))


def test_a_version_tag_with_a_full_digest_is_accepted():
    assert v.image_pin_problems(v.parse_image(f"ghcr.io/berriai/litellm:v1.108.5@{AMD}")) == []


def test_the_repo_compose_file_is_pinned_by_digest():
    refs = [r for r in v.compose_images((ROOT / "docker-compose.yml").read_text(encoding="utf-8")) if r.is_litellm]
    assert len(refs) == 1 and v.image_pin_problems(refs[0]) == [] and refs[0].tag == "v" + v.pinned_version()


# ---------------------------------------------------------------- --image against a fake registry

def test_image_matches_the_registry(repo):
    body = index()
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(body)}")
    registry = FakeRegistry(body)
    findings = v.check_image(repo, registry)
    assert levels(findings) == [v.OK, v.OK, v.OK], text(findings)
    assert AMD in text(findings) and ARM in text(findings)
    token_url, manifest_url = registry.calls[0][0], registry.calls[1][0]
    assert token_url.startswith("https://ghcr.io/token?") and "repository:berriai/litellm:pull" in token_url
    assert manifest_url == "https://ghcr.io/v2/berriai/litellm/manifests/v1.108.5"
    assert registry.calls[1][1]["Authorization"] == "Bearer anon" and "oci.image.index" in registry.calls[1][1]["Accept"]
    assert all(method == "GET" for _, _, method in registry.calls)  # read-only


def test_a_moved_tag_is_a_problem(repo):
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{AMD}")
    findings = v.check_image(repo, FakeRegistry(index()))
    assert v.PROBLEM in levels(findings) and "should never move" in text(findings)
    assert v.exit_code(findings) == 1


def test_the_known_bad_digest_from_the_registry_is_a_problem(repo, monkeypatch):
    body = index()
    monkeypatch.setattr(v, "KNOWN_BAD_DIGESTS", frozenset({digest_of(body)}))
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{AMD}")
    assert "listed as compromised" in text(v.check_image(repo, FakeRegistry(body)))


def test_a_known_bad_platform_image_is_a_problem(repo):
    body = index(platforms=(("linux", "amd64", BAD), ("linux", "arm64", ARM)))
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(body)}")
    findings = v.check_image(repo, FakeRegistry(body))
    assert "linux/amd64 image" in text(findings) and "known-bad" in text(findings) and v.exit_code(findings) == 1


def test_a_missing_arm64_image_is_a_problem(repo):
    body = index(platforms=(("linux", "amd64", AMD),))
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(body)}")
    findings = v.check_image(repo, FakeRegistry(body))
    assert "no linux/arm64 image" in text(findings) and v.exit_code(findings) == 1


def test_an_unpinned_or_floating_image_is_a_problem_without_asking_the_registry(repo):
    compose(repo, "ghcr.io/berriai/litellm:main-latest")
    registry = FakeRegistry(index())
    findings = v.check_image(repo, registry)
    assert v.exit_code(findings) == 1 and "floating" in text(findings) and "no @sha256" in text(findings)
    assert registry.calls == []


@pytest.mark.parametrize("registry", [
    FakeRegistry(raises=True),
    FakeRegistry(index(), token_status=503),
    FakeRegistry(index(), manifest_status=404),
    FakeRegistry(index(), manifest_status=500),
    FakeRegistry(index(), stated="sha256:" + "d" * 64),  # header and body disagree: trust neither
    FakeRegistry(b"not json"),
])
def test_an_unreachable_or_odd_registry_is_could_not_check_never_ok(repo, registry):
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(registry.body or index())}")
    findings = v.check_image(repo, registry)
    assert v.exit_code(findings) == 2 and v.OK not in levels(findings), text(findings)
    assert "was not compared with the registry" in text(findings)


def test_other_registries_and_missing_files_are_could_not_check(repo):
    assert v.exit_code(v.check_image(repo, FakeRegistry(index()))) == 2  # no compose file at all
    compose(repo, f"registry.example.com/mirror/litellm:v1.108.5@{AMD}")
    findings = v.check_image(repo, FakeRegistry(index()))
    assert v.exit_code(findings) == 2 and "no anonymous lookup" in text(findings)


def test_the_spend_compose_file_is_checked_too_and_other_images_are_ignored(repo):
    body = index()
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(body)}")
    (repo / "docker-compose.spend.yml").write_text("services:\n  db:\n    image: postgres:16-alpine\n  litellm:\n    image: litellm/litellm:latest\n")
    findings = v.check_image(repo, FakeRegistry(body))
    assert "docker-compose.spend.yml" in text(findings) and v.exit_code(findings) == 1
    assert "postgres" not in text(findings)


def test_http_fetch_refuses_plain_http():
    with pytest.raises(v.CannotCheck):
        v.http_fetch("http://ghcr.io/v2/", {}, "GET")


# ---------------------------------------------------------------- the installed package

def test_clean_install_matches_the_lock(tmp_path, repo):
    site = install(tmp_path / "site")
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert levels(findings) == [v.OK, v.OK, v.NOTE], text(findings)
    assert "3 installed litellm files match" in text(findings)  # the .pyc and RECORD rows carry no hash
    assert "cannot be recomputed" in text(findings)  # says what it could not check
    assert v.exit_code(findings) == 0


def test_a_file_changed_after_install_is_a_problem(tmp_path, repo):
    site = install(tmp_path / "site")
    (site / "litellm" / "main.py").write_bytes(b"import os\n")
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert v.exit_code(findings) == 1 and "litellm/main.py" in text(findings)


def test_a_file_removed_after_install_is_a_problem(tmp_path, repo):
    site = install(tmp_path / "site")
    (site / "litellm" / "main.py").unlink()
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert v.exit_code(findings) == 1 and "are gone" in text(findings)


def test_the_compromised_version_is_a_problem(tmp_path, repo):
    site = install(tmp_path / "site", version="1.107.0")
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert v.exit_code(findings) == 1 and "compromised release" in text(findings) and "rotate" in text(findings)


def test_a_version_other_than_the_pin_is_a_problem(tmp_path, repo):
    site = install(tmp_path / "site", version="1.109.0")
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert v.exit_code(findings) == 1 and "the repo pins 1.108.5" in text(findings)


def test_not_installed_is_could_not_check_or_a_note_with_image(tmp_path, repo):
    site = install(tmp_path / "site", name="somethingelse")
    lock = repo / "requirements" / "proxy.lock"
    assert v.exit_code(v.check_package([site], lock)) == 2
    assert levels(v.check_package([site], lock, required=False)) == [v.NOTE]


def test_no_record_and_no_lock_pin_are_could_not_check(tmp_path, repo):
    site = install(tmp_path / "site", record=False)
    findings = v.check_package([site], repo / "requirements" / "proxy.lock")
    assert v.exit_code(findings) == 2 and "no RECORD" in text(findings)
    site2 = install(tmp_path / "site2")
    findings = v.check_package([site2], tmp_path / "absent.lock")
    assert v.exit_code(findings) == 2 and "no litellm pin" in text(findings)


def test_a_similarly_named_package_is_not_litellm(tmp_path, repo):
    site = install(tmp_path / "site", name="litellm-proxy-extras", version="0.4.110")
    assert v.find_litellm([site]) == []


def test_pinned_version_reads_the_repo_lock():
    assert v.pinned_version() == "1.108.5"
    lock = (ROOT / "requirements" / "proxy.lock").read_text(encoding="utf-8")
    assert "litellm==1.107.0" not in lock


# ---------------------------------------------------------------- indicator files

def test_no_indicators(tmp_path):
    site = install(tmp_path / "site")
    findings = v.check_indicators([site], tmp_path / "remote_code_loader.lock")
    assert levels(findings) == [v.OK] and "does not prove" in text(findings)


@pytest.mark.parametrize("rel", ["cook.pth", "cook.py", "sitecustomize.py", "__pycache__/cook.cpython-312.pyc",
                                 "__pycache__/sitecustomize.cpython-313.pyc", "litellm-1.107.0.dist-info/METADATA"])
def test_each_indicator_file_is_a_problem(tmp_path, rel):
    site = install(tmp_path / "site")
    target = site / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"")
    findings = v.check_indicators([site], tmp_path / "remote_code_loader.lock")
    assert v.exit_code(findings) == 1 and v.OK not in levels(findings), text(findings)
    assert str(site) in text(findings)


def test_the_runtime_lock_file_is_a_problem(tmp_path):
    site = install(tmp_path / "site")
    lock = tmp_path / "remote_code_loader.lock"
    lock.write_bytes(b"")
    findings = v.check_indicators([site], lock)
    assert v.exit_code(findings) == 1 and "remote-code loader" in text(findings)


def test_the_real_lock_path_is_the_one_the_incident_page_names():
    assert str(v.RUNTIME_LOCK) == "/tmp/remote_code_loader.lock"
    assert set(v.INDICATOR_FILES) == {"cook.pth", "cook.py", "sitecustomize.py"}
    assert v.known_bad(BAD) and not v.known_bad(AMD) and not v.known_bad(None)


def test_no_site_directory_is_could_not_check(tmp_path):
    assert v.exit_code(v.check_indicators([], tmp_path / "x.lock")) == 2


# ---------------------------------------------------------------- the command

def run_main(args, repo, tmp_path, fetch=None):
    lines: list[str] = []
    code = v.main(args, fetch=fetch or FakeRegistry(raises=True), out=lines.append, root=repo,
                  runtime_lock=tmp_path / "remote_code_loader.lock")
    return code, "\n".join(lines)


def test_main_clean_is_0_and_never_touches_the_network_without_image(tmp_path, repo):
    site = install(tmp_path / "site")
    registry = FakeRegistry(index())
    code, out = run_main(["--site-packages", str(site)], repo, tmp_path, registry)
    assert code == 0 and "result: nothing wrong found" in out and registry.calls == []


def test_main_problem_is_1(tmp_path, repo):
    site = install(tmp_path / "site")
    (site / "cook.pth").write_text("import cook\n")
    code, out = run_main(["--site-packages", str(site)], repo, tmp_path)
    assert code == 1 and "[PROBLEM]" in out and "result: PROBLEM found" in out


def test_main_could_not_check_is_2(tmp_path, repo):
    site = install(tmp_path / "site", name="somethingelse")
    code, out = run_main(["--site-packages", str(site)], repo, tmp_path)
    assert code == 2 and "could not be checked" in out
    code, out = run_main(["--site-packages", str(tmp_path / "nowhere")], repo, tmp_path)
    assert code == 2 and "not a directory" in out


def test_main_with_image(tmp_path, repo):
    body = index()
    compose(repo, f"ghcr.io/berriai/litellm:v1.108.5@{digest_of(body)}")
    site = install(tmp_path / "site")
    code, out = run_main(["--image", "--site-packages", str(site)], repo, tmp_path, FakeRegistry(body))
    assert code == 0 and "still resolves to the pinned" in out
    # A Docker host has no litellm installed: with --image that is a note, and the image check decides.
    empty = install(tmp_path / "empty", name="somethingelse")
    code, out = run_main(["--image", "--site-packages", str(empty)], repo, tmp_path, FakeRegistry(body))
    assert code == 0 and "[note] litellm is not installed" in out
    # A registry that cannot be reached is 2, not 0.
    code, out = run_main(["--image", "--site-packages", str(site)], repo, tmp_path, FakeRegistry(raises=True))
    assert code == 2
    # A problem outranks could-not-check.
    (site / "cook.py").write_text("")
    code, out = run_main(["--image", "--site-packages", str(site)], repo, tmp_path, FakeRegistry(raises=True))
    assert code == 1


def test_the_script_is_standard_library_only_and_read_only():
    source = (ROOT / "scripts" / "verify_litellm.py").read_text(encoding="utf-8")
    imports = {line.split()[1].split(".")[0] for line in source.splitlines() if line.startswith(("import ", "from "))}
    assert imports <= set(sys.stdlib_module_names) | {"__future__"}
    for banned in ("subprocess", "os.environ", "API_KEY", ".unlink(", ".write_text(", ".write_bytes(", "open(\"w", "method=\"POST\"", "shutil"):
        assert banned not in source, banned


# ---------------------------------------------------------------- the validator's rules (scripts/validate_config.py)

def test_validator_install_and_image_rules():
    pytest.importorskip("yaml")
    import validate_config as vc

    install_cmd = "pip" + " install "
    assert vc.install_line_problems(install_cmd + "litellm[proxy]")
    assert vc.install_line_problems("RUN " + install_cmd + "--no-cache-dir 'litellm>=1.100,<2'")
    assert not vc.install_line_problems(install_cmd + "'litellm[proxy]==1.108.5'")
    assert not vc.install_line_problems(install_cmd + "--require-hashes -r requirements/proxy.lock")
    assert vc.unpinned_litellm("litellm==1.*") and not vc.unpinned_litellm("litellm-enterprise")
    vc.scenario_supply_chain()  # the repo as it stands passes


def test_validator_refuses_an_unpinned_compose_image(tmp_path, monkeypatch):
    pytest.importorskip("yaml")
    import validate_config as vc

    for name in ("docker-compose.spend.yml",):
        (tmp_path / name).write_text((ROOT / name).read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / "docker-compose.yml").write_text("services:\n  litellm:\n    image: ghcr.io/berriai/litellm:v1.108.5\n")
    monkeypatch.setattr(vc, "ROOT", tmp_path)
    with pytest.raises(vc.Failure, match="no @sha256"):
        vc.scenario_supply_chain()
    (tmp_path / "docker-compose.yml").write_text(f"services:\n  litellm:\n    image: ghcr.io/berriai/litellm:main-stable@{AMD}\n")
    with pytest.raises(vc.Failure, match="floating"):
        vc.scenario_supply_chain()
