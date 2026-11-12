"""Load Lean problems from bundled samples or a formal-conjectures checkout.

google-deepmind/formal-conjectures is *not* vendored: it is a large,
separately licensed repository. Point ``MUSE_FORMAL_CONJECTURES`` at a
local clone and this loader will pick up ``.lean`` files that still
contain a *real* ``sorry`` (not one inside a comment). The bundled
samples under ``muse/useful_work/samples/`` are original, tiny theorems
so CI does not need that clone or a Lean toolchain.

Ids are unique: samples use the ``-- problem_id:`` header (or
``{stem}.{theorem}``); formal-conjectures files use
``{relative/path.lean}#{theorem_name}`` so two files with the same
stem cannot collide. Imports and module preamble are stored on the
``Problem`` and prepended when the verifier assembles a file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from muse.useful_work.banned import strip_comments_and_strings

HEADER_ID = re.compile(r"^--\s*problem_id:\s*(.+?)\s*$", re.MULTILINE)
HEADER_TITLE = re.compile(r"^--\s*title:\s*(.+?)\s*$", re.MULTILINE)

# A theorem or lemma whose hole is ``sorry``, possibly after ``:= by``
# or as a term ``:= sorry``, with attributes and doc comments before it.
# We search on the original text after identifying holes on a
# comment-stripped copy, so ``sorry`` in comments does not count.
THEOREM_START = re.compile(
    r"(?P<prefix>(?:^|\n)(?:[ \t]*@[^\n]+\n)*)"
    r"(?P<kw>theorem|lemma)\s+(?P<name>[A-Za-z0-9_'.]+)",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Problem:
    problem_id: str
    title: str
    path: Path
    statement: str
    theorem_name: str
    source: str  # "sample" | "formal-conjectures"
    preamble: str = ""
    namespace: str = ""

    def prompt(self) -> str:
        statement = self.statement.rstrip()
        header = (
            "You are a Lean 4 prover. Complete the following theorem by replacing "
            "`sorry` with a valid Lean 4 proof. Return only the proof body "
            "(tactics or a term), not a new theorem declaration. "
            "Do not change the statement. Return only Lean code, no markdown."
        )
        preamble = self.preamble.strip()
        block = (preamble + "\n\n" if preamble else "") + statement
        return header + "\n\n" + block + "\n"


def load_problems(
    samples_dir: Path,
    formal_conjectures_dir: Path | None = None,
) -> list[Problem]:
    problems: list[Problem] = []
    seen: set[str] = set()
    if samples_dir.is_dir():
        for path in sorted(samples_dir.glob("*.lean")):
            for item in _from_file(path, source="sample"):
                item = _unique(item, seen)
                problems.append(item)
                seen.add(item.problem_id)
    if formal_conjectures_dir and formal_conjectures_dir.is_dir():
        root = formal_conjectures_dir
        for path in sorted(formal_conjectures_dir.rglob("*.lean")):
            # Skip lakefile / build outputs / mathlib copies if present.
            parts = set(path.parts)
            if parts & {".lake", "lake-packages", ".git", "build"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            if not _has_real_sorry(text):
                continue
            for item in _from_file(
                path,
                source="formal-conjectures",
                text=text,
                root=root,
            ):
                item = _unique(item, seen)
                problems.append(item)
                seen.add(item.problem_id)
    return problems


def get_problem(problems: list[Problem], problem_id: str) -> Problem:
    for item in problems:
        if item.problem_id == problem_id:
            return item
    known = ", ".join(p.problem_id for p in problems) or "(none)"
    raise KeyError(f"unknown problem {problem_id!r}; known: {known}")


def _has_real_sorry(text: str) -> bool:
    stripped = strip_comments_and_strings(text)
    return re.search(r"\bsorry\b", stripped) is not None


def _from_file(
    path: Path,
    *,
    source: str,
    text: str | None = None,
    root: Path | None = None,
) -> list[Problem]:
    body = text if text is not None else path.read_text(encoding="utf-8")
    header_id = _search(HEADER_ID, body)
    title = _search(HEADER_TITLE, body) or _title_from_module(body, path)
    preamble, namespace = _preamble_and_namespace(body)
    found: list[Problem] = []
    for index, (kw, name, statement) in enumerate(_iter_sorry_theorems(body)):
        pid = _make_id(
            path,
            source=source,
            name=name,
            root=root,
            header_id=header_id if index == 0 else None,
        )
        found.append(
            Problem(
                problem_id=pid,
                title=title if index == 0 else f"{title} ({name})",
                path=path,
                statement=statement.strip(),
                theorem_name=name,
                source=source,
                preamble=preamble,
                namespace=namespace,
            )
        )
    if not found and _has_real_sorry(body):
        # A file with a hole we could not parse as a theorem — keep it
        # as a last-resort problem so formal-conjectures files with an
        # unusual shape are still listed (the verifier will fail-closed
        # if it cannot lock a statement).
        pid = _make_id(
            path,
            source=source,
            name=path.stem,
            root=root,
            header_id=header_id,
        )
        found.append(
            Problem(
                problem_id=pid,
                title=title,
                path=path,
                statement=body.strip(),
                theorem_name=header_id or path.stem,
                source=source,
                preamble=preamble,
                namespace=namespace,
            )
        )
    return found


def _make_id(
    path: Path,
    *,
    source: str,
    name: str,
    root: Path | None,
    header_id: str | None,
) -> str:
    if header_id:
        return header_id
    if source == "formal-conjectures" and root is not None:
        try:
            rel = path.relative_to(root).as_posix()
        except ValueError:
            rel = path.name
        return f"{rel}#{name}"
    return f"{path.stem}.{name}"


def _unique(item: Problem, seen: set[str]) -> Problem:
    if item.problem_id not in seen:
        return item
    n = 2
    while f"{item.problem_id}#{n}" in seen:
        n += 1
    return Problem(
        problem_id=f"{item.problem_id}#{n}",
        title=item.title,
        path=item.path,
        statement=item.statement,
        theorem_name=item.theorem_name,
        source=item.source,
        preamble=item.preamble,
        namespace=item.namespace,
    )


def _iter_sorry_theorems(body: str) -> list[tuple[str, str, str]]:
    """Return ``(keyword, name, statement_including_sorry)`` for each hole."""
    stripped = strip_comments_and_strings(body)
    found: list[tuple[str, str, str]] = []
    for match in THEOREM_START.finditer(body):
        kw = match.group("kw")
        name = match.group("name")
        start = match.start("kw")
        # The hole is the first ``sorry`` in the comment-stripped text
        # after this theorem and before the next theorem-level declaration.
        next_decl = _next_declaration(body, match.end())
        region = body[start:next_decl]
        region_stripped = strip_comments_and_strings(region)
        hole = re.search(r"\bsorry\b", region_stripped)
        if not hole:
            continue
        # Include through the end of that sorry (plus a trailing newline).
        end = start + hole.end()
        statement = body[start:end]
        # If attributes precede the keyword, include them.
        attr_start = match.start("prefix")
        # ``prefix`` may start with a newline; trim a leading newline only.
        if match.group("prefix").strip():
            statement = body[match.start("prefix"):end].lstrip("\n")
        found.append((kw, name, statement.rstrip() + "\n"))
    return found


def _next_declaration(body: str, after: int) -> int:
    """Index of the next top-level theorem/lemma/def/instance, or len(body)."""
    pattern = re.compile(
        r"(?m)^(theorem|lemma|def|instance|structure|class|inductive|axiom)\s+"
    )
    for match in pattern.finditer(body, after):
        return match.start()
    return len(body)


def _preamble_and_namespace(body: str) -> tuple[str, str]:
    """Imports, opens, and set_option lines before the first theorem."""
    stripped = strip_comments_and_strings(body)
    first = None
    for match in THEOREM_START.finditer(body):
        first = match.start("kw")
        break
    head = body[:first] if first is not None else body
    # Keep import / open / set_option / universe / namespace lines.
    keep: list[str] = []
    namespace = ""
    for raw in head.splitlines():
        line = raw.strip()
        # Skip copyright and module docs; keep Lean commands.
        if line.startswith("import ") or line.startswith("open ") or line.startswith("set_option "):
            keep.append(raw.rstrip())
        elif line.startswith("universe ") or line.startswith("variable "):
            keep.append(raw.rstrip())
        elif line.startswith("namespace "):
            namespace = line.split(None, 1)[1].strip()
            keep.append(raw.rstrip())
        elif line.startswith("public import ") or line.startswith("import"):
            keep.append(raw.rstrip())
    return "\n".join(keep).rstrip(), namespace


def _title_from_module(body: str, path: Path) -> str:
    # formal-conjectures files often start with a ``/-! # Title`` module doc.
    match = re.search(r"/-!\s*\n#\s+(.+?)\n", body)
    if match:
        return match.group(1).strip()
    return path.stem.replace("_", " ")


def _search(pattern: re.Pattern[str], text: str) -> str | None:
    match = pattern.search(text)
    return match.group(1).strip() if match else None
