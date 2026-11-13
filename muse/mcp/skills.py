"""Where Muse skills get connected to the tools server. Not connected yet.

The skills package (muse/skills/) is being built separately. The tools server
must reach skills the way it reaches everything else, through the home, and
the home control API has no skills call yet. So `list_skills` and `run_skill`
answer "not built" until someone connects a backend here.

To connect it (the integrator's job, one place):

    from muse.mcp import skills
    skills.connect(backend)      # before the server starts, e.g. in muse/mcp/server.py::build_context

where `backend` has two methods and returns plain data only:

    backend.list()                          -> [{"name": str, "summary": str}, ...]
    backend.run(name: str, inputs: dict)    -> {"status": str, ...}

A backend should call the home (a future `GET /v1/skills`, `POST /v1/skills/{name}/run`)
rather than import the runner: this process holds no provider key and must not
gain one. Nothing here imports muse.skills internals; it only checks whether
the package can be imported, to say which of two things is missing.
"""

from __future__ import annotations

import importlib.util
from typing import Any, Protocol


class SkillsBackend(Protocol):
    def list(self) -> list[dict[str, Any]]:  # pragma: no cover - protocol
        ...

    def run(self, name: str, inputs: dict[str, Any]) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


_backend: SkillsBackend | None = None


def connect(backend: SkillsBackend | None) -> None:
    global _backend
    _backend = backend


def backend() -> SkillsBackend | None:
    return _backend


def why_not_built() -> str:
    try:
        present = importlib.util.find_spec("muse.skills") is not None
    except (ImportError, ValueError):
        present = False
    if present:
        return "skills are not connected to host bots yet"
    return "skills are not built yet"
