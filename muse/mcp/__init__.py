"""The Muse tools server: Muse's capabilities as Model Context Protocol tools for a host bot.

A host bot (docs/hosts/README.md) starts this as a child process and talks to it
over stdin/stdout:

    python -m muse.mcp --token-file ~/.muse/host-tools.token      (also: muse mcp serve)

It is a client of the home control API (muse/home/client.py) and nothing else.
It holds no provider key, and never opens the ledger, the queue, the card store
or the key store itself. What it may do is what its device token may do.

    muse.mcp.tools        the tools: schemas, validation, and what each one asks the home
    muse.mcp.permission   the interim rule behind `check_permission`
    muse.mcp.skills       the one place skills get connected later
    muse.mcp.server       the protocol glue (the official `mcp` SDK) and start-up checks
    muse.mcp.hosts        `muse hosts ...`: setup snippets and the read-only config check
    muse.mcp.cli          the `muse mcp` and `muse hosts` commands

Tool names are a contract with every host that has them in a prompt; do not rename one.
"""

SERVER_NAME = "muse"
TOKEN_ENV = "MUSE_HOME_TOKEN"

# Said in every result that touches approval or permission, word for word (docs/spec/05-host-bots.md section 4).
SCOPE_NOTE = "Muse controls model use, spending and its own tools; it cannot stop this host's own shell or file tools."
