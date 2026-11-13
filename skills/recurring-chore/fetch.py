"""Fetching a web address. NEEDS THE SANDBOX: not available in this version.

A skill that declares `net` hosts is refused by the runner until the sandbox
exists, because nothing could hold it to that list. So the feed template reads
files, and every address it is given goes through this interface, which today
only says why it cannot be read. When the sandbox package lands, a fetcher
that runs inside it replaces `NoFetcher` and the template does not change.
"""

from __future__ import annotations


class NeedsSandbox(Exception):
    """This address cannot be fetched: Muse has no sandbox to fetch it in yet."""


class Fetcher:
    """What the feed template asks for the text behind an address."""

    available = False

    def fetch(self, url: str, limit_bytes: int) -> str:
        raise NotImplementedError


class NoFetcher(Fetcher):
    """Today's fetcher: nothing is fetched, nothing leaves this computer."""

    available = False

    def fetch(self, url: str, limit_bytes: int) -> str:
        raise NeedsSandbox("fetching web addresses needs the sandbox, which is not built yet")


def fetcher() -> Fetcher:
    return NoFetcher()
