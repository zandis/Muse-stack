"""Fail-closed static checks on Lean source.

A proof is rejected if, after comments and string literals are stripped,
it contains a construct that can make Lean report success unsoundly, or
that performs elaboration-time IO. These checks run in every mode,
including the mock (which never emits ``verified``) and in CI where the
Lean comparator is unavailable.

Banned constructs (identifiers / attributes / options, not raw
substrings — so a comment saying "sorry" or a name like ``sorryAx`` in
a comment is ignored, but an identifier ``sorryful`` that starts with
the token ``sorry`` as a whole word is still caught when the token is
exactly ``sorry`` / ``admit``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Whole-token bans. Matched as Lean identifiers or after ``@`` / ``set_option``.
BANNED_TOKENS = frozenset(
    {
        "sorry",
        "admit",
        "native_decide",
        "nativedecide",  # folded
        "ofReduceBool",
        "ofreducebool",
        "ofReduceNat",
        "ofreducenat",
        "implemented_by",
        "implementedby",
        "extern",
        "uncheckedCast",
        "uncheckedcast",
        "unsafe",
    }
)

# Multi-word / dotted forms searched on the comment-stripped, whitespace-
# normalised source.
BANNED_PHRASES = (
    "debug.skipKernelTC",
    "debug.skipkerneltc",
    "set_option debug.skipKernelTC",
    "implemented_by",
    "native_decide",
    "nativeDecide",
    "ofReduceBool",
    "ofReduceNat",
    # Elaboration-time IO. Lean 4 will run these while compiling.
    "IO.FS",
    "IO.Process",
    "IO.FS.Handle",
    "run_cmd",
    "run_elab",
    "elab_rules",
    "init_proc",
)

# A leading ``axiom`` declaration (not ``#print axioms`` / ``Propext``).
AXIOM_DECL = re.compile(r"(?m)^\s*axiom\b")

# ``#eval`` / ``#run`` that might execute IO at elaboration time.
EVAL_CMD = re.compile(r"(?m)^\s*#(eval|run|check_failure|synth)\b")

# IO. in any form after stripping.
IO_USE = re.compile(r"\bIO\b")

# Identifier tokenizer: Lean allows letters, digits, underscore, apostrophe,
# and ``?`` / ``!``. Dotted names are split.
IDENT = re.compile(r"[A-Za-z_\u03b1-\u03c9][A-Za-z0-9_'\u03b1-\u03c9?!]*")


@dataclass(frozen=True)
class BanHit:
    reason: str
    token: str


def strip_comments_and_strings(source: str) -> str:
    """Replace Lean comments and string literals with spaces (length-preserving
    enough for line structure: newlines in block comments are kept).

    Handles ``--`` line comments, nested ``/- -/`` block comments (including
    doc ``/--`` and module ``/-!``), and ``"..."`` / ``'...'`` literals with
    basic escapes. This is intentionally conservative: if we cannot parse,
    we leave the text in place so a banned token inside a broken literal
    still gets caught (fail-closed).
    """
    out: list[str] = []
    i = 0
    n = len(source)
    while i < n:
        ch = source[i]
        # Line comment
        if ch == "-" and i + 1 < n and source[i + 1] == "-":
            # But ``---`` in some docs; still a comment. Not ``s[i-1]=='-'`` from `/--`.
            # Look back: if we just started a block we wouldn't be here.
            j = i + 2
            while j < n and source[j] != "\n":
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        # Block comment, nested. ``/-``, ``/--``, ``/-!``
        if ch == "/" and i + 1 < n and source[i + 1] == "-":
            depth = 1
            j = i + 2
            while j < n and depth:
                if source[j] == "/" and j + 1 < n and source[j + 1] == "-":
                    depth += 1
                    j += 2
                    continue
                if source[j] == "-" and j + 1 < n and source[j + 1] == "/":
                    depth -= 1
                    j += 2
                    continue
                j += 1
            # Keep newlines so line-oriented checks still work; spaces otherwise.
            chunk = source[i:j]
            out.append("".join("\n" if c == "\n" else " " for c in chunk))
            i = j
            continue
        # String literal
        if ch == '"':
            j = i + 1
            while j < n:
                if source[j] == "\\" and j + 1 < n:
                    j += 2
                    continue
                if source[j] == '"':
                    j += 1
                    break
                j += 1
            out.append(" " * (j - i))
            i = j
            continue
        # Character literal ``'x'``
        if ch == "'" and i + 2 < n and source[i + 2] == "'" and source[i + 1] != "'":
            out.append("   ")
            i += 3
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def find_banned(source: str) -> list[BanHit]:
    """Return every banned construct found in ``source``. Empty means it passed."""
    stripped = strip_comments_and_strings(source)
    hits: list[BanHit] = []
    folded = stripped
    for match in IDENT.finditer(stripped):
        token = match.group(0)
        folded_token = token.replace("_", "").lower()
        if token.lower() in BANNED_TOKENS or folded_token in BANNED_TOKENS:
            # ``sorryAx`` is a different identifier (sorryAx) and should not
            # match. We only match the exact token.
            hits.append(BanHit(reason="banned_token", token=token))
    lower = stripped.lower()
    for phrase in BANNED_PHRASES:
        if phrase.lower() in lower:
            hits.append(BanHit(reason="banned_phrase", token=phrase))
    if AXIOM_DECL.search(stripped):
        hits.append(BanHit(reason="axiom_declaration", token="axiom"))
    if EVAL_CMD.search(stripped) and IO_USE.search(stripped):
        hits.append(BanHit(reason="elaboration_io", token="#eval IO"))
    # A bare ``#eval`` of a non-IO term is usually fine, but fail-closed
    # on any ``#eval`` / ``#run``: they run at elaboration time.
    ev = EVAL_CMD.search(stripped)
    if ev:
        hits.append(BanHit(reason="elaboration_command", token=ev.group(0).strip()))
    # Deduplicate by (reason, token) keeping order.
    seen: set[tuple[str, str]] = set()
    unique: list[BanHit] = []
    for hit in hits:
        key = (hit.reason, hit.token)
        if key not in seen:
            seen.add(key)
            unique.append(hit)
    return unique


def banned_message(hits: list[BanHit]) -> str:
    if not hits:
        return ""
    parts = [f"{h.reason}:{h.token}" for h in hits]
    return "rejected banned Lean construct(s): " + ", ".join(parts)
