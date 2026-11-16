#!/usr/bin/env python3
"""Put, get, list and delete objects in one Object Storage bucket as this instance.

No stored API key: the VM proves who it is with the certificate Oracle places in
its metadata service and rotates several times a day ("instance principal").
A dynamic group and a policy in the tenancy decide what that identity may do
(deploy/oracle-stack/iam.tf: read the backup bucket and manage its objects,
nothing else).

    oci_object.py put FILE OBJECT_NAME
    oci_object.py get OBJECT_NAME FILE
    oci_object.py list [PREFIX]          # JSON lines: name, size, timeCreated
    oci_object.py delete OBJECT_NAME

Settings: MUSE_BACKUP_NAMESPACE and MUSE_BACKUP_BUCKET (environment).

Why this file and not the OCI command line tool: that tool is a large Python
package with its own dependency tree, which would need a second hash-locked
list to stay inside the supply-chain rule. This needs only `cryptography`,
which requirements/proxy.lock already pins by hash, and the standard library.

How it works (the same steps Oracle's SDKs take; see oci-python-sdk,
oci/auth/federation_client.py and oci/auth/signers/security_token_signer.py):

1. Read the leaf certificate, its private key and the intermediate certificate
   from http://169.254.169.254/opc/v2/identity/ (header "Authorization: Bearer
   Oracle"). They are held in memory only.
2. Make a one-off RSA key pair in memory. POST the certificates and the new
   public key to https://auth.<region>.<realm>/v1/x509, signed with the leaf
   key (keyId "<tenancy>/fed-x509/<SHA-1 fingerprint>"). The answer is a
   short-lived token.
3. Sign each Object Storage request with the one-off private key and keyId
   "ST$<token>" (draft HTTP Signatures, rsa-sha256).

NOT TESTED against Oracle. It was written without access to a tenancy; the
tests run it against a local stand-in that checks every signature. Treat the
first run on a real VM as the test.
"""

from __future__ import annotations

import base64
import email.utils
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Iterator

IMDS_DEFAULT = "http://169.254.169.254/opc/v2"
TIMEOUT_S = 60


class OciError(RuntimeError):
    pass


def _crypto():
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import padding, rsa
    except ImportError as exc:  # pragma: no cover - depends on the environment
        raise OciError("the 'cryptography' package is needed (it is in requirements/proxy.lock)") from exc
    return x509, hashes, serialization, padding, rsa


def _check_url(url: str) -> str:
    """HTTPS everywhere, except the metadata service and a loopback stand-in used by tests."""
    parts = urllib.parse.urlsplit(url)
    if parts.scheme == "https":
        return url
    if parts.scheme == "http" and parts.hostname in ("169.254.169.254", "127.0.0.1"):
        return url
    raise OciError(f"refusing a URL that is not https: {url}")


def _open(request: urllib.request.Request) -> tuple[int, bytes]:
    # No proxy and no redirects: these calls go to Oracle's own endpoints only.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
    try:
        with opener.open(request, timeout=TIMEOUT_S) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()[:500].decode("utf-8", "replace")
        raise OciError(f"{request.get_method()} {urllib.parse.urlsplit(request.full_url).path}: HTTP {exc.code} {body}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise OciError(f"{request.get_method()} {urllib.parse.urlsplit(request.full_url).netloc}: {exc}") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *_args, **_kwargs):  # noqa: D401, ANN002, ANN003
        return None


def _strip_pem(pem: bytes) -> str:
    return "".join(line for line in pem.decode("ascii").splitlines() if line and not line.startswith("-----"))


def signature_header(key_id: str, private_key, method: str, url: str, headers: dict[str, str], names: list[str]) -> str:
    """The Authorization value for a request, signing the listed headers in order."""
    _x509, hashes, _ser, padding, _rsa = _crypto()
    parts = urllib.parse.urlsplit(url)
    target = parts.path + (f"?{parts.query}" if parts.query else "")
    lines = []
    for name in names:
        value = f"{method.lower()} {target}" if name == "(request-target)" else headers[name]
        lines.append(f"{name}: {value}")
    signature = private_key.sign("\n".join(lines).encode("utf-8"), padding.PKCS1v15(), hashes.SHA256())
    return (f'Signature algorithm="rsa-sha256",headers="{" ".join(names)}",keyId="{key_id}",'
            f'signature="{base64.b64encode(signature).decode("ascii")}",version="1"')


class InstancePrincipal:
    """A short-lived token for this instance, and the key that signs with it."""

    def __init__(self, environ: dict[str, str] | None = None) -> None:
        env = dict(os.environ if environ is None else environ)
        self.imds = env.get("MUSE_OCI_IMDS", IMDS_DEFAULT).rstrip("/")
        self._auth_override = env.get("MUSE_OCI_AUTH_ENDPOINT", "").rstrip("/")
        self._os_override = env.get("MUSE_OCI_OS_ENDPOINT", "").rstrip("/")
        self.region = ""
        self.realm = ""
        self.token = ""
        self.session_key = None

    def _imds(self, path: str) -> bytes:
        request = urllib.request.Request(_check_url(f"{self.imds}/{path}"), headers={"Authorization": "Bearer Oracle"})
        return _open(request)[1]

    def _load_region(self) -> None:
        if self.region:
            return
        doc = json.loads(self._imds("instance/"))
        self.region = str(doc.get("canonicalRegionName") or doc.get("region") or "")
        self.realm = str((doc.get("regionInfo") or {}).get("realmDomainComponent") or "oraclecloud.com")
        if not self.region:
            raise OciError("the metadata service did not name a region")

    @property
    def auth_endpoint(self) -> str:
        self._load_region()
        return self._auth_override or f"https://auth.{self.region}.{self.realm}"

    @property
    def object_endpoint(self) -> str:
        self._load_region()
        return self._os_override or f"https://objectstorage.{self.region}.{self.realm}"

    def login(self) -> None:
        """Exchange the instance certificate for a token. Keys stay in memory."""
        x509, hashes, serialization, _padding, rsa = _crypto()
        leaf_pem = self._imds("identity/cert.pem")
        leaf_key_pem = self._imds("identity/key.pem")
        intermediate_pem = self._imds("identity/intermediate.pem")
        leaf = x509.load_pem_x509_certificate(leaf_pem)
        leaf_key = serialization.load_pem_private_key(leaf_key_pem, password=None)
        tenancy = ""
        for attribute in leaf.subject:
            value = str(attribute.value)
            if value.startswith("opc-tenant:"):
                tenancy = value.split(":", 1)[1]
                break
            if value.startswith("opc-identity:") and not tenancy:
                tenancy = value.split(":", 1)[1]
        if not tenancy:
            raise OciError("the instance certificate does not name a tenancy")
        fingerprint = ":".join(f"{byte:02X}" for byte in leaf.fingerprint(hashes.SHA1()))  # noqa: S303 - the protocol's key id, not a security hash

        self.session_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        public_pem = self.session_key.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
        body = json.dumps({
            "certificate": _strip_pem(leaf_pem),
            "publicKey": _strip_pem(public_pem),
            "intermediateCertificates": [_strip_pem(intermediate_pem)],
        }).encode("utf-8")
        url = _check_url(f"{self.auth_endpoint}/v1/x509")
        headers = {
            "date": email.utils.formatdate(usegmt=True),
            "content-type": "application/json",
            "content-length": str(len(body)),
            "x-content-sha256": base64.b64encode(hashlib.sha256(body).digest()).decode("ascii"),
        }
        headers["authorization"] = signature_header(
            f"{tenancy}/fed-x509/{fingerprint}", leaf_key, "POST", url, headers,
            ["date", "(request-target)", "content-length", "content-type", "x-content-sha256"])
        _status, answer = _open(urllib.request.Request(url, data=body, headers=headers, method="POST"))
        try:
            self.token = str(json.loads(answer)["token"])
        except (ValueError, KeyError, TypeError):
            raise OciError("the identity service answered without a token") from None

    def request(self, method: str, path: str, query: dict[str, str] | None = None, data=None, length: int | None = None) -> tuple[int, bytes]:
        """One signed Object Storage call. `data` may be bytes or an open binary file (give `length`)."""
        if not self.token:
            self.login()
        url = _check_url(self.object_endpoint + path + (("?" + urllib.parse.urlencode(query)) if query else ""))
        headers = {"date": email.utils.formatdate(usegmt=True), "host": urllib.parse.urlsplit(url).netloc}
        if data is not None:
            headers["content-type"] = "application/octet-stream"
            headers["content-length"] = str(len(data) if length is None else length)
        # Object uploads are signed without the body, as Oracle's SDKs do for PutObject.
        headers["authorization"] = signature_header(f"ST${self.token}", self.session_key, method, url, headers, ["date", "(request-target)", "host"])
        return _open(urllib.request.Request(url, data=data, headers=headers, method=method))


class Bucket:
    def __init__(self, namespace: str, bucket: str, principal: InstancePrincipal | None = None) -> None:
        if not namespace or not bucket:
            raise OciError("MUSE_BACKUP_NAMESPACE and MUSE_BACKUP_BUCKET must be set")
        self.base = f"/n/{urllib.parse.quote(namespace, safe='')}/b/{urllib.parse.quote(bucket, safe='')}"
        self.principal = principal or InstancePrincipal()

    def _object(self, name: str) -> str:
        return f"{self.base}/o/{urllib.parse.quote(name, safe='')}"

    def put(self, file_path: str, name: str) -> None:
        with open(file_path, "rb") as handle:
            self.principal.request("PUT", self._object(name), data=handle, length=os.fstat(handle.fileno()).st_size)

    def get(self, name: str, file_path: str) -> None:
        _status, body = self.principal.request("GET", self._object(name))
        tmp = f"{file_path}.part"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(body)
        os.replace(tmp, file_path)

    def list(self, prefix: str = "") -> Iterator[dict]:
        start = ""
        while True:
            query = {"fields": "name,size,timeCreated", "limit": "1000"}
            if prefix:
                query["prefix"] = prefix
            if start:
                query["start"] = start
            _status, body = self.principal.request("GET", f"{self.base}/o", query=query)
            doc = json.loads(body)
            yield from doc.get("objects") or []
            start = doc.get("nextStartWith") or ""
            if not start:
                return

    def delete(self, name: str) -> None:
        self.principal.request("DELETE", self._object(name))


def main(argv: list[str]) -> int:
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__.strip())
        return 0 if argv else 2
    try:
        bucket = Bucket(os.environ.get("MUSE_BACKUP_NAMESPACE", ""), os.environ.get("MUSE_BACKUP_BUCKET", ""))
        command, args = argv[0], argv[1:]
        if command == "put" and len(args) == 2:
            bucket.put(args[0], args[1])
        elif command == "get" and len(args) == 2:
            bucket.get(args[0], args[1])
        elif command == "list" and len(args) <= 1:
            for item in bucket.list(args[0] if args else ""):
                print(json.dumps(item, sort_keys=True))
        elif command == "delete" and len(args) == 1:
            bucket.delete(args[0])
        else:
            print("usage: oci_object.py put FILE NAME | get NAME FILE | list [PREFIX] | delete NAME", file=sys.stderr)
            return 2
    except OciError as exc:
        print(f"oci_object: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
