"""The citation rule, shared by the extract step (drop a claim at once) and the final check.

It is the files-answer rule, with the same numbers: a statement passes when it
cites passages that were given, at least 30% of its terms appear in those
passages taken together, at least 15% in each one, and at least 2 terms are
shared (1 for a statement with fewer than 4 terms). Terms come from textwork.py
(words, whole numbers, character pairs for Chinese). tests/test_skill_research.py
holds this rule to the files-answer check on the same examples.

Results are short codes; nothing here returns text from a passage or a claim.
"""

from __future__ import annotations

from .textwork import overlap, terms

MIN_SHARE = 0.30
MIN_SHARE_EACH = 0.15
MIN_SHARED = 2

OK = ""
NO_CITE = "no-cite"
UNKNOWN = "unknown-source"
NOTHING = "nothing-to-check"
WEAK = "weak-overlap"


def supported(text: str, cites: list[int], passages: dict[int, str]) -> tuple[str, int]:
    """(code, the source number the code is about or 0). OK ("") when the statement stands on what it cites."""
    if not cites:
        return NO_CITE, 0
    missing = [n for n in cites if n not in passages]
    if missing:
        return UNKNOWN, missing[0]
    mine = terms(text)
    if not mine:
        return NOTHING, 0
    need = MIN_SHARED if len(mine) >= 4 else 1
    for n in cites:
        shared, share = overlap(text, passages[n])
        if shared < need or share < MIN_SHARE_EACH:
            return WEAK, n
    shared, share = overlap(text, "\n".join(passages[n] for n in cites))
    if shared < need or share < MIN_SHARE:
        return WEAK, 0
    return OK, 0
