"""`python -m muse.mcp`: the Muse tools server on stdin/stdout."""

from muse.mcp.server import main

if __name__ == "__main__":
    raise SystemExit(main())
