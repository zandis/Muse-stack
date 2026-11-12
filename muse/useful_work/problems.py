"""Load Lean problems from bundled samples or a formal-conjectures checkout.

google-deepmind/formal-conjectures is *not* vendored: it is a large, separately
licensed repository. Point ``MUSE_FORMAL_CONJECTURES`` at a local clone and
this loader will pick up ``.lean`` files that still contain ``sorry``. The
bundled samples under ``muse/useful_work/samples/`` are original, tiny
theorems so CI does not need that clone or a Lean toolchain.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADER_ID = re.compile(r"^--\s*problem_id:\s*(.+?)\s*$", re.MULTILINE)
HEADER_TITLE = re.compile(r"^--\s*title:\s*(.+?)\s*$", re.MULTILINE)
THEOREM = re.compile(
    r"(?P<kw>theorem|lemma)\s+(?P<name>[A-Za-z0-9_'.]+)\s*(?P<rest>.*?)by\s+sorry",
    re.DOTALL,
)


@dataclass(frozen=True)
class Problem:
    problem_id: str
    title: str
    path: Path
    statement: str
    theorem_name: str
    source: str  # "sample" | "formal-conjectures"

    def prompt(self) -> str:
        statement = self.statement.rstrip()
        header = (
            "You are a Lean 4 prover. Complete the following theorem by replacing "
            "`sorry` with a valid Lean 4 proof. Return only Lean code, no markdown."
        )
        return header + "\n\n" + statement + "\n"


def load_problems(
    samples_dir: Path,
    formal_conjectures_dir: Path | None = None,
) -> list[Problem]:
    problems: list[Problem] = []
    if samples_dir.is_dir():
        for path in sorted(samples_dir.glob("*.lean")):
            problems.extend(_from_file(path, source="sample"))
    if formal_conjectures_dir and formal_conjectures_dir.is_dir():
        for path in sorted(formal_conjectures_dir.rglob("*.lean")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if "sorry" not in text:
                continue
            problems.extend(_from_file(path, source="formal-conjectures", text=text))
    return problems


def get_problem(problems: list[Problem], problem_id: str) -> Problem:
    for item in problems:
        if item.problem_id == problem_id:
            return item
    known = ", ".join(p.problem_id for p in problems) or "(none)"
    raise KeyError(f"unknown problem {problem_id!r}; known: {known}")


def _from_file(path: Path, *, source: str, text: str | None = None) -> list[Problem]:
    body = text if text is not None else path.read_text(encoding="utf-8")
    header_id = _search(HEADER_ID, body)
    title = _search(HEADER_TITLE, body) or path.stem.replace("_", " ")
    found = []
    for index, match in enumerate(THEOREM.finditer(body)):
        name = match.group("name")
        pid = header_id if (header_id and index == 0) else f"{path.stem}.{name}"
        if header_id and index == 0:
            pid = header_id
        statement = match.group(0).strip()
        found.append(
            Problem(
                problem_id=pid,
                title=title if index == 0 else f"{title} ({name})",
                path=path,
                statement=statement,
                theorem_name=name,
                source=source,
            )
        )
    if not found and "sorry" in body:
        found.append(
            Problem(
                problem_id=header_id or path.stem,
                title=title,
                path=path,
                statement=body.strip(),
                theorem_name=path.stem,
                source=source,
            )
        )
    return found


def _search(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None
