"""The runner's own app on the router: `skills` (never paid) and `skills-paid` (only when a job's budget allows money).

Like the queue's managed app (muse/queue.py::ensure_app): the token is shown
nowhere, lives in the key store, and is re-issued if the registry no longer
matches it. Priority `normal`: a skill run is something the person asked for,
but it keeps the small reserve free for the app they are typing in. Later the
helper runtime mints these tokens from permissions instead.
"""

from __future__ import annotations

from typing import Mapping, MutableMapping

from muse import apps, keys

SKILLS_APP = "skills"
SKILLS_PAID_APP = "skills-paid"
TOKEN_VARS = {SKILLS_APP: "MUSE_SKILLS_APP_TOKEN", SKILLS_PAID_APP: "MUSE_SKILLS_PAID_APP_TOKEN"}
PRIORITY = "normal"


def app_for(paid: bool) -> str:
    return SKILLS_PAID_APP if paid else SKILLS_APP


def ensure_app(name: str, environ: Mapping[str, str]) -> str:
    """The token for managed app `name`, creating or repairing the app first."""
    want_paid = name == SKILLS_PAID_APP
    token = str(environ.get(TOKEN_VARS[name], "") or "").strip()
    app = apps.load_apps(environ).get(name)
    if app is not None and app.allow_paid != want_paid:
        app = apps.update_app(name, environ, allow_paid=want_paid)  # paid use is chosen per job, never inherited
    if app is not None and token and apps.hash_token(token) == app.token_sha256:
        return token
    if app is None:
        _, token = apps.add_app(name, PRIORITY, allow_paid=want_paid, environ=environ,
                                note="managed by muse skills" + (" (jobs with a money budget only)" if want_paid else ""))
    else:
        _, token = apps.rotate_token(name, environ)
    keys.set_key(TOKEN_VARS[name], token, force=True, environ=environ)
    if isinstance(environ, MutableMapping):
        environ[TOKEN_VARS[name]] = token
    return token
