"""The control pipe between the parent (which keeps the router client) and the sandboxed child.

A frame is a 4-byte big-endian length followed by that many bytes of UTF-8
JSON holding one object with a string `op`. Nothing else is ever accepted.
Bytes travel as base64 text. Both sides refuse a frame longer than their cap
BEFORE reading it, so a hostile peer cannot make the other allocate.

The parent reads and writes with a deadline (`Channel`); the child blocks
(`BlockingChannel`). The parent treats every byte from the child as untrusted.
"""

from __future__ import annotations

import base64
import json
import os
import select
import struct
import time
from typing import Any, Callable

HEADER = struct.Struct(">I")
MAX_DEPTH = 64


class ProtocolError(Exception):
    """The peer sent something that is not a frame of this protocol. The message is a short code, never content."""


class ChannelClosed(Exception):
    """The peer closed the pipe (it exited or crashed)."""


class ChannelTimeout(Exception):
    """The deadline passed while waiting for the peer."""


def encode(obj: dict[str, Any]) -> bytes:
    body = json.dumps(obj, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return HEADER.pack(len(body)) + body


def decode(body: bytes) -> dict[str, Any]:
    try:
        obj = json.loads(body.decode("utf-8"))
    except RecursionError:
        raise ProtocolError("too-deep") from None
    except (UnicodeDecodeError, ValueError):
        raise ProtocolError("not-json") from None
    if not isinstance(obj, dict) or not isinstance(obj.get("op"), str) or not obj["op"] or len(obj["op"]) > 40:
        raise ProtocolError("not-a-frame")
    return obj


def b64(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def unb64(text: Any) -> bytes:
    if not isinstance(text, str):
        raise ProtocolError("not-base64")
    try:
        return base64.b64decode(text.encode("ascii"), validate=True)
    except (ValueError, UnicodeEncodeError):
        raise ProtocolError("not-base64") from None


class Channel:
    """The parent's end: non-blocking, every call takes an absolute deadline (a `time.monotonic()` value)."""

    def __init__(self, read_fd: int, write_fd: int, max_frame: int, *, tick: Callable[[], None] | None = None,
                 tick_every: float = 1.0) -> None:
        self.read_fd, self.write_fd, self.max_frame = read_fd, write_fd, max_frame
        self._tick, self._tick_every = tick, tick_every
        self.bytes_in = 0
        os.set_blocking(read_fd, False)
        os.set_blocking(write_fd, False)

    def _wait(self, fd: int, write: bool, deadline: float) -> None:
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                raise ChannelTimeout()
            ready = select.select([] if write else [fd], [fd] if write else [], [], min(left, self._tick_every))
            if ready[0] or ready[1]:
                return
            if self._tick is not None:
                self._tick()  # the parent's once-a-second checks (data folder size); may raise

    def _read_exact(self, count: int, deadline: float) -> bytes:
        chunks: list[bytes] = []
        while count:
            self._wait(self.read_fd, False, deadline)
            try:
                chunk = os.read(self.read_fd, min(count, 1 << 20))
            except BlockingIOError:
                continue
            except OSError:
                raise ChannelClosed() from None
            if not chunk:
                raise ChannelClosed()
            chunks.append(chunk)
            count -= len(chunk)
            self.bytes_in += len(chunk)
        return b"".join(chunks)

    def recv(self, deadline: float) -> dict[str, Any]:
        (length,) = HEADER.unpack(self._read_exact(HEADER.size, deadline))
        if length > self.max_frame:
            raise ProtocolError("frame-too-large")
        if length < 2:
            raise ProtocolError("not-a-frame")
        return decode(self._read_exact(length, deadline))

    def send(self, obj: dict[str, Any], deadline: float) -> None:
        view = memoryview(encode(obj))
        while view:
            self._wait(self.write_fd, True, deadline)
            try:
                sent = os.write(self.write_fd, view[: 1 << 20])
            except BlockingIOError:
                continue
            except OSError:
                raise ChannelClosed() from None
            view = view[sent:]

    def close(self) -> None:
        for fd in (self.read_fd, self.write_fd):
            try:
                os.close(fd)
            except OSError:
                pass


class BlockingChannel:
    """The child's end. Blocking; a closed pipe means the parent is gone and the child exits."""

    def __init__(self, read_fd: int, write_fd: int, max_frame: int) -> None:
        self.read_fd, self.write_fd, self.max_frame = read_fd, write_fd, max_frame

    def _read_exact(self, count: int) -> bytes:
        chunks: list[bytes] = []
        while count:
            chunk = os.read(self.read_fd, min(count, 1 << 20))
            if not chunk:
                raise ChannelClosed()
            chunks.append(chunk)
            count -= len(chunk)
        return b"".join(chunks)

    def recv(self) -> dict[str, Any]:
        (length,) = HEADER.unpack(self._read_exact(HEADER.size))
        if length > self.max_frame or length < 2:
            raise ProtocolError("frame-too-large")
        return decode(self._read_exact(length))

    def send(self, obj: dict[str, Any]) -> None:
        view = memoryview(encode(obj))
        while view:
            try:
                sent = os.write(self.write_fd, view[: 1 << 20])
            except OSError:
                raise ChannelClosed() from None
            view = view[sent:]
