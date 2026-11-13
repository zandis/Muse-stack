"""Start-up checks and the protocol glue for the Muse tools server.

Protocol: the Model Context Protocol over stdio, through the official Python
SDK (`mcp`, 2.x). The SDK does the lifecycle (initialize, or the newer
per-request form), framing and version negotiation; this file only maps
`tools/list` and `tools/call` onto muse/mcp/tools.py.

What this file is careful about:
  * stdout belongs to the protocol. Everything else goes to stderr.
  * The log says which tool ran, how it ended and how long it took. Never an
    argument, a result, a token or exception text.
  * The device token comes from a 0600 file (`--token-file`) or from
    MUSE_HOME_TOKEN. Never from argv, and it is never printed. A token file
    that anyone else can read stops the server before it starts.
  * A tool call never raises into the SDK (which would send exception text to
    the host); muse.mcp.tools.call always returns a result.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import sys
import time
from pathlib import Path
from typing import Any, Mapping

from muse.mcp import SERVER_NAME, TOKEN_ENV, tools

log = logging.getLogger("muse.mcp")

MAX_TOKEN_FILE_BYTES = 4096
ALLOW_READ_ENV = "MUSE_MCP_ALLOW_READ"
MAX_ALLOW_READ = 64

INSTRUCTIONS = (
    "Muse is the person's own layer for AI use: it rations free allowances, caps spending, and asks the person before "
    "things that write, send or spend. Use allowance_status before large work; ask_approval before any action that "
    "writes, sends or spends, and act only on 'approved'; queue_job for work that can wait. "
    + tools.SCOPE_NOTE
)


class StartupError(Exception):
    """Why the server will not start. The message is safe to print: it never holds the token or file contents."""


def read_token_file(path: Path) -> str:
    """The token in a private file. Refuses a file others can read, a link, or anything that is not one short line."""
    try:
        info = os.lstat(path)
    except OSError:
        raise StartupError("the token file cannot be found or read") from None
    if not stat.S_ISREG(info.st_mode):
        raise StartupError("the token file must be a regular file, not a link or a folder")
    if os.name == "posix":
        if info.st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise StartupError("the token file can be read by other users; run `chmod 600` on it")
        if hasattr(os, "getuid") and info.st_uid != os.getuid():
            raise StartupError("the token file belongs to another user")
    if info.st_size > MAX_TOKEN_FILE_BYTES:
        raise StartupError("the token file is too large to be a token")
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        raise StartupError("the token file cannot be found or read") from None
    return _clean_token(text, "the token file")


def _clean_token(text: str, where: str) -> str:
    if not text or len(text) > 512 or any(ch.isspace() or ord(ch) < 32 for ch in text):
        raise StartupError(f"{where} must hold exactly one token on one line")
    return text


def load_token(token_file: str | None, environ: Mapping[str, str]) -> str:
    if token_file:
        return read_token_file(Path(token_file).expanduser())
    value = environ.get(TOKEN_ENV, "")
    if not value.strip():
        raise StartupError(f"no device token: give --token-file PATH (a 0600 file) or set {TOKEN_ENV}")
    return _clean_token(value.strip(), TOKEN_ENV)


def build_context(args: argparse.Namespace, environ: Mapping[str, str]) -> tools.Context:
    from muse.home.client import HomeClient

    token = load_token(args.token_file, environ)
    client = HomeClient(token, socket_path=args.socket, tcp_port=args.tcp, environ=environ, timeout=10.0)
    listed = list(args.allow_read or [])
    listed += [p for p in environ.get(ALLOW_READ_ENV, "").split(os.pathsep) if p.strip()]
    if len(listed) > MAX_ALLOW_READ or any(len(p) > 400 for p in listed):
        raise StartupError(f"too many or too long --allow-read entries (at most {MAX_ALLOW_READ})")
    return tools.Context(home=tools.HomeLink(client), allowed_read=tuple(listed))


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python -m muse.mcp",
        description="The Muse tools server for a host bot (Model Context Protocol over stdin/stdout).",
    )
    add_arguments(p)
    return p


def add_arguments(p: argparse.ArgumentParser) -> None:
    p.add_argument("--token-file", metavar="PATH",
                   help=f"a 0600 file holding the device token (from `muse home token add`); otherwise {TOKEN_ENV} is read")
    p.add_argument("--socket", metavar="PATH", help="the home's Unix socket (default MUSE_HOME/home.sock)")
    p.add_argument("--tcp", type=int, metavar="PORT", help="reach the home on 127.0.0.1:PORT instead of the socket")
    p.add_argument("--allow-read", action="append", metavar="PATH",
                   help="something this host may read without asking (repeatable); used only by check_permission")


def _setup_logging() -> None:
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter("muse-mcp %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(logging.WARNING)
    log.setLevel(logging.INFO)


def make_server(ctx: tools.Context) -> Any:
    """The SDK server with the two handlers. Imported here so the rest of muse.mcp works without the SDK."""
    import anyio
    import mcp_types as types
    from mcp.server.lowlevel import Server

    from muse import __version__

    listing = types.ListToolsResult(tools=[
        types.Tool(
            name=t.name, title=t.title, description=t.description, input_schema=t.input_schema,
            annotations=types.ToolAnnotations(read_only_hint=t.read_only, destructive_hint=False, open_world_hint=False),
        )
        for t in tools.TOOLS
    ])

    async def on_list_tools(_ctx: Any, _params: Any) -> Any:
        return listing

    async def on_call_tool(_ctx: Any, params: Any) -> Any:
        started = time.monotonic()
        name = params.name if isinstance(params.name, str) else ""
        known = name if name in tools.BY_NAME else "(unknown)"
        try:
            result, is_error = await anyio.to_thread.run_sync(tools.call, ctx, name, params.arguments, abandon_on_cancel=True)
        except Exception as exc:  # the worker could not run at all; tools.call itself never raises
            log.error("tool=%s outcome=crash type=%s", known, type(exc).__name__)
            result, is_error = tools.ToolError("internal", "The Muse tools server hit a problem it did not expect.").payload(), True
        outcome = result["error"]["code"] if is_error else "ok"
        log.info("tool=%s outcome=%s ms=%d", known, outcome, (time.monotonic() - started) * 1000)
        text = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        return types.CallToolResult(content=[types.TextContent(type="text", text=text)], structured_content=result, is_error=is_error)

    return Server(SERVER_NAME, version=__version__, title="Muse", instructions=INSTRUCTIONS,
                  on_list_tools=on_list_tools, on_call_tool=on_call_tool)


async def _serve(ctx: tools.Context) -> None:
    from mcp.server.stdio import stdio_server

    server = make_server(ctx)
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(read_stream, write_stream, server.create_initialization_options())
    finally:
        ctx.stop.set()  # a tool still waiting for the person lets go at once


def run(args: argparse.Namespace, environ: Mapping[str, str] | None = None) -> int:
    """Serve until stdin closes. Returns a process exit code."""
    _setup_logging()
    environ = os.environ if environ is None else environ
    try:
        ctx = build_context(args, environ)
    except StartupError as err:
        print(f"muse-mcp: not started: {err}.", file=sys.stderr)
        return 2
    try:
        import anyio
        import mcp  # noqa: F401 - the official SDK
    except ImportError:
        print("muse-mcp: not started: the `mcp` package is not installed. Install it with `pip install 'muse-stack[mcp]'`.",
              file=sys.stderr)
        return 3
    log.info("started tools=%d token=%s", len(tools.TOOLS), "file" if args.token_file else "env")
    try:
        anyio.run(_serve, ctx)
    except KeyboardInterrupt:
        pass
    except Exception as exc:  # never a traceback with anything from a request in it
        log.error("stopped type=%s", type(exc).__name__)
        return 1
    finally:
        ctx.stop.set()
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(parser().parse_args(argv))
