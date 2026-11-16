"""Skills, first form: plain Python run by a simple runner on the home.

A skill is a folder with `SKILL.md` (docs/skills.md). It reaches a model only
through `muse.skills.llm.Client`, which talks to the local router's
OpenAI-compatible endpoint with the runner's app token; it never holds a
provider key. The helper runtime with permissions, a sandbox and confirm
cards (packages A11, A12) will wrap this runner; skills written against
`Context` do not change.

What this first form does NOT provide: isolation. A skill is Python code
running in the runner's process, as the person. `needs` is declared, shown
before approval, and enforced on every path the `Workspace` hands out, but a
skill that ignores the `Workspace` is not stopped until the sandbox exists.
"""

from muse.skills.format import FormatError, Skill, parse_skill_md  # noqa: F401
from muse.skills.llm import (  # noqa: F401
    Answer, Budget, BudgetExceeded, Client, Deferred, Options, PrivacyRefused, RouterError,
)
from muse.skills.loader import LoaderError, approve, discover, find  # noqa: F401
from muse.skills.runner import Context, Result, apply_writes, run  # noqa: F401
from muse.skills.workspace import NeedsViolation, ProposedWrite, Workspace  # noqa: F401
