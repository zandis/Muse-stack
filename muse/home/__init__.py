"""The home control API: the one way an app talks to the home.

A small local HTTP/JSON service (standard library only). See
`docs/spec/interfaces/home-api.v1.yaml` for the contract and
`docs/spec/01-architecture.md` section 4.1 for where it sits.

    muse.home.api      the route table, handlers, and the `Home` behind them
    muse.home.server   the transport: a Unix socket (0600) or 127.0.0.1
    muse.home.auth     device tokens (hash only) behind a `verify(token)` interface
    muse.home.cards    the pending-approval store (cards.sqlite)
    muse.home.fake     canned data for app development (`python -m muse.home --fake`)
    muse.home.client   a tiny client, used by `muse home call` and the tests
"""

API_VERSION = "1"
