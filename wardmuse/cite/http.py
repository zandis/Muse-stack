"""HTTP for the citation lookup. Standard library only.

The hosts this module will call are fixed. A redirect is refused, so a 3xx
cannot leave that list. NCBI E-utilities are paced at or below 3 requests a
second whether or not ``NCBI_API_KEY`` is set. The key is read from the
environment only when it is present, and it is never put in an error message.
"""

from __future__ import annotations

import os
import re
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Mapping
from urllib.parse import urlsplit

TOOL = "wardmuse"
DEFAULT_EMAIL = "wardmuse-cite@example.invalid"
MIN_INTERVAL_S = 1.0 / 3.0  # NCBI's no-key ceiling; this tool never goes faster
MAX_LIMIT = 20
MAX_QUERY = 300
USER_AGENT = "wardmuse-cite/0.1 (public citation lookup; no patient data)"

# Hosts verified 2026-11-17. Nothing else is contacted.
ALLOWED_HOSTS = frozenset(
    {
        "eutils.ncbi.nlm.nih.gov",
        "www.ebi.ac.uk",
        "data.fda.gov.tw",
        "info.nhi.gov.tw",
    }
)

Fetch = Callable[[str, float], tuple[int, bytes]]


class CiteError(Exception):
    """A lookup failure with a fixed sentence. The sentence never holds a query, a key or a body."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuse every redirect. A 3xx must not hop onto another host."""

    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        try:
            fp.read(64)
        finally:
            fp.close()
        raise CiteError("redirect", "The citation service redirected. Nothing was followed.")


def check_url(url: str) -> None:
    """Raise unless ``url`` is https on one of the citation hosts, with no userinfo."""

    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    if parts.scheme != "https" or host not in ALLOWED_HOSTS or parts.username or parts.password:
        raise CiteError("refused", "That address is not one of the citation sources.")


def _opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(_NoRedirect)


def default_get(url: str, timeout: float, *, max_bytes: int = 2_000_000, opener: Any = None) -> tuple[int, bytes]:
    """GET ``url``. Returns ``(status, body)``. Raises :class:`CiteError` for a redirect or a foreign host."""

    check_url(url)
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json, application/xml, text/csv, */*"}, method="GET")
    chosen = opener or _opener()
    try:
        with chosen.open(request, timeout=timeout) as response:  # noqa: S310 (host checked above)
            status = int(getattr(response, "status", 200))
            body = response.read(max_bytes + 1)
    except CiteError:
        raise
    except urllib.error.HTTPError as exc:
        status = int(exc.code)
        try:
            body = exc.read(max_bytes + 1)
        except OSError:
            body = b""
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        raise CiteError("unavailable", "The citation source could not be reached.") from None
    if len(body) > max_bytes:
        raise CiteError("too_large", "The citation source sent more than this tool will read.")
    return status, body


class Pace:
    """Space calls so they stay at or below ``per_second`` (default 3)."""

    def __init__(self, per_second: float = 3.0, *, sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.monotonic) -> None:
        if per_second <= 0 or per_second > 3:
            raise ValueError("NCBI calls stay at or below 3 per second")
        self.min_interval = 1.0 / per_second
        self._sleep = sleep
        self._clock = clock
        self._next = 0.0
        self._lock = threading.Lock()

    def wait(self) -> None:
        with self._lock:
            now = self._clock()
            delay = self._next - now
            if delay > 0:
                self._sleep(delay)
                now = self._clock()
            self._next = now + self.min_interval


def clean_query(query: str, *, limit: int) -> str:
    """The query this tool will send, or a :class:`CiteError` that does not repeat it."""

    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= MAX_LIMIT:
        raise CiteError("invalid_input", "The limit must be a whole number from 1 to 20.")
    if not isinstance(query, str) or "\x00" in query:
        raise CiteError("invalid_input", "The query must be text.")
    text = " ".join(query.split())
    if not text:
        raise CiteError("invalid_input", "The query is empty.")
    if len(text) > MAX_QUERY:
        raise CiteError("invalid_input", "The query is too long.")
    from wardmuse.catalog import looks_secret  # the same detector the home uses

    if looks_secret(text):
        raise CiteError("refused", "The query was refused because it looks like a key or token.")
    return text


_EMAIL = re.compile(r"^[A-Za-z0-9._%+-]{1,64}@[A-Za-z0-9.-]{1,190}$")


def ncbi_params(environ: Mapping[str, str] | None) -> dict[str, str]:
    """``tool`` and ``email``, plus ``api_key`` only when ``NCBI_API_KEY`` is set and usable.

    ``environ`` defaults to the process environment. Pass a mapping in tests so a
    key sitting in the real environment is not read.
    """

    env = os.environ if environ is None else environ
    email = str(env.get("NCBI_EMAIL", "") or "").strip()
    params = {"tool": TOOL, "email": email if _EMAIL.fullmatch(email) else DEFAULT_EMAIL}
    key = str(env.get("NCBI_API_KEY", "") or "").strip()
    if key and " " not in key and len(key) <= 128 and not any(c in key for c in "&?#="):
        params["api_key"] = key
    return params


def call(fetch: Fetch, url: str, timeout: float, *, what: str) -> bytes:
    """Run ``fetch`` and return the body of a 200. Errors name ``what``, never the URL."""

    try:
        status, body = fetch(url, timeout)
    except CiteError:
        raise
    except (OSError, ValueError, TimeoutError):
        raise CiteError("unavailable", f"{what} could not be reached.") from None
    if status == 429:
        raise CiteError("too_many", f"{what} asked us to slow down. Wait a moment and try again.")
    if status != 200 or not body:
        raise CiteError("unavailable", f"{what} did not answer.")
    return body
