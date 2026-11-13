"""The home control API: the one way an app talks to the home.

A small local HTTP/JSON service (standard library only). See
`docs/spec/interfaces/home-api.v1.yaml` for the contract and
`docs/spec/01-architecture.md` section 4.1 for where it sits.

    muse.home.api      the route table, handlers, and the `Home` behind them
    muse.home.server   the transport: a Unix socket (0600) or 127.0.0.1
    muse.home.auth     device tokens (hash only) behind a `verify(token)` interface; roles and their scopes
    muse.home.cards    the pending-approval store (cards.sqlite)
    muse.home.state    who queued which job, skill runs and the files they proposed (home-state.sqlite)
    muse.home.folders  the folders a skill may be given by name (`muse skills folders`)
    muse.home.skills   skills through the API: runs, and writing a proposed file once its card is approved
    muse.home.fake     canned data for app development (`python -m muse.home --fake`)
    muse.home.client   a tiny client, used by `muse home call` and the tests
"""

API_VERSION = "1"
