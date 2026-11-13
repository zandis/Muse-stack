"""The permission compiler (architecture section 2.8, work package C7).

A permission is a signed token (muse/permissions). This package makes what ENFORCES one, tied to its id,
and takes it away again:

    plan.py    plan(mandate) -> [ArtifactSpec]      pure: what each enforcing system needs
    core.py    Compiler: issue, compile, apply, status, revoke, sweep; the artifact table's rules
    table.py   MUSE_HOME/compiler.sqlite: artifacts, usage, approvals waiting on a card, drafts, requests
    gate.py    the router's check: is the permission behind this app key still in force? (no extra library)
    guard.py   the skill runner's check: MUSE_PERMISSIONS=off|advise|enforce
    hosts.py   a host bot's permission: the default one, and the answer behind the `check_permission` tool
    cli.py     `muse permissions grant | stop | status | sweep`

Built targets: skills (narrowed tokens, folders), router (an app entry), records (consent and revocation).
Not built, and shown as such: payments, connectors, health.

Nothing is imported here on purpose: the proxy imports `muse.compiler.gate` on its request path, and that
must not pull in the rest.
"""
