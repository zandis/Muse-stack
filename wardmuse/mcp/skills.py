"""Where Muse skills are connected to the tools server: through the home, like everything else.

`connect(backend)` is the one place. `muse/mcp/server.py::build_context` calls it
with `HomeSkills(link)`, which turns the three skill tools into three calls on
the home control API:

    list_skills     GET  /v1/skills
    run_skill       POST /v1/skills/{name}/run      then GET /v1/skills/runs/{id} until it is done
    skill_status    GET  /v1/skills/runs/{id}

Nothing here imports the skill runner or its internals. This process holds no
provider key and must not gain one; the home runs the skill, checks what the
host asked for (a folder by NAME from the person's list, never a path; nothing
above the skill's own budget) and writes a proposed file only after the person
approved it. Until a backend is connected the tools answer "not built".

A backend returns plain data only:

    backend.list()                                  -> {"skills": [...], "folders": [...]}
    backend.run(name, inputs, folders, out)         -> the run, as the home describes it
    backend.status(run_id)                          -> the run
"""

from __future__ import annotations

import importlib.util
from typing import Any, Protocol, Sequence


class SkillsBackend(Protocol):
    def list(self) -> dict[str, Any]:  # pragma: no cover - protocol
        ...

    def run(self, name: str, inputs: dict[str, Any], folders: Sequence[str] = (), out: str | None = None) -> dict[str, Any]:  # pragma: no cover
        ...

    def status(self, run_id: str) -> dict[str, Any]:  # pragma: no cover - protocol
        ...


class HomeSkills:
    """The backend the tools server uses: the home control API. `link` is muse.mcp.tools.HomeLink."""

    def __init__(self, link: Any) -> None:
        self.link = link

    def list(self) -> dict[str, Any]:
        _, payload = self.link.call("GET", "/v1/skills")
        return payload

    def run(self, name: str, inputs: dict[str, Any], folders: Sequence[str] = (), out: str | None = None) -> dict[str, Any]:
        body: dict[str, Any] = {"inputs": inputs}
        if folders:
            body["folders"] = list(folders)
        if out is not None:
            body["out"] = out
        _, payload = self.link.call("POST", f"/v1/skills/{name}/run", body)
        return payload.get("run") if isinstance(payload.get("run"), dict) else {}

    def status(self, run_id: str) -> dict[str, Any]:
        _, payload = self.link.call("GET", f"/v1/skills/runs/{run_id}")
        return payload.get("run") if isinstance(payload.get("run"), dict) else {}


_backend: SkillsBackend | None = None


def connect(backend: SkillsBackend | None) -> None:
    global _backend
    _backend = backend


def backend() -> SkillsBackend | None:
    return _backend


def why_not_built() -> str:
    try:
        present = importlib.util.find_spec("wardmuse.skills") is not None or importlib.util.find_spec("muse.skills") is not None
    except (ImportError, ValueError):
        present = False
    if present:
        return "skills are not connected to host bots yet"
    return "skills are not built yet"
