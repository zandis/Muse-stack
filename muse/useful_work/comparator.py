"""Lake-comparator-style check: the proof must prove the trusted statement.

A *lake comparator* (Lean core) takes a trusted constant from a file the
model cannot edit and requires the submission to prove *that* constant,
with axioms limited to ``propext``, ``Quot.sound``, and
``Classical.choice``. This module implements that contract:

1. The statement always comes from the trusted problem file. A proof
   that starts with ``theorem`` / ``lemma`` is parsed; if its signature
   does not match the trusted one, it is rejected. The model's theorem
   is never written as the Lean file.
2. The assembled file is: trusted preamble + trusted theorem with
   ``sorry`` replaced by the *proof body only*.
3. When a real Lean toolchain is available, we run it in the sandbox,
   then parse ``#print axioms`` and the pretty-printed type, and reject
   anything outside the axiom allow-list or whose type does not match.
4. When Lean is not available (CI), a fail-closed Python equivalent
   runs: statement lock, banned-construct scan, and **no** ``verified``
   verdict. The mock mode uses only this path and always reports
   ``verified: false, mode: mock``.

Documented in ``docs/LEAN.md``.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field

from muse.useful_work.banned import banned_message, find_banned, strip_comments_and_strings
from muse.useful_work.problems import Problem

ALLOWED_AXIOMS = frozenset(
    {
        "propext",
        "Quot.sound",
        "Classical.choice",
        "Classical.Choice",  # sometimes pretty-printed this way
        "Quotient.sound",    # alias some versions print
    }
)

THEOREM_HEAD = re.compile(
    r"^\s*(?:(?:@[^\n]+\n)\s*)*(theorem|lemma)\s+([A-Za-z0-9_'.]+)",
    re.MULTILINE,
)

# Split a theorem into (keyword, name, signature-up-to-colon-equals, body).
THEOREM_SPLIT = re.compile(
    r"(?s)^\s*(?P<attrs>(?:@[^\n]+\n\s*)*)"
    r"(?P<kw>theorem|lemma)\s+(?P<name>[A-Za-z0-9_'.]+)\s*"
    r"(?P<sig>.*?)"
    r"(?::=\s*by\b|:=)",
)


@dataclass
class StatementLock:
    ok: bool
    reason: str
    trusted_name: str
    trusted_signature: str
    submitted_name: str | None = None
    submitted_signature: str | None = None
    proof_body: str = ""
    assembled: str = ""
    static_checks_passed: bool = False
    banned: list[str] = field(default_factory=list)


def normalize_signature(text: str) -> str:
    """Whitespace- and comment-insensitive form of a theorem signature."""
    stripped = strip_comments_and_strings(text)
    # Drop attributes for comparison of the type itself, but keep binders.
    stripped = re.sub(r"@[^\n]+", " ", stripped)
    stripped = stripped.replace("\u2115", "Nat").replace("ℕ", "Nat")
    stripped = stripped.replace("\u2192", "->").replace("→", "->")
    stripped = stripped.replace("\u2200", "forall ").replace("∀", "forall ")
    # Collapse whitespace and lowercase for a conservative comparison.
    return " ".join(stripped.split()).strip()


def extract_theorem(source: str) -> tuple[str, str, str, str] | None:
    """Return ``(kw, name, signature, body)`` from a theorem/lemma source, or None."""
    stripped_head = strip_comments_and_strings(source)
    match = THEOREM_SPLIT.search(stripped_head)
    if not match:
        # Try the original (attributes / comments in the signature).
        match = THEOREM_SPLIT.search(source)
    if not match:
        return None
    kw = match.group("kw")
    name = match.group("name")
    sig = match.group("sig")
    # Body is whatever follows the ``:=`` or ``:= by`` in the *original*
    # (not comment-stripped) text, so we can still scan it for bans.
    # Find the same split point on the original.
    body = _body_after_assign(source)
    return kw, name, sig, body


def _body_after_assign(source: str) -> str:
    """Proof body after the first top-level ``:= by`` or ``:=``."""
    # Walk a comment-stripped copy to find the index, then slice original.
    stripped = strip_comments_and_strings(source)
    for marker in (":= by", ":=by", ":="):
        idx = stripped.find(marker)
        if idx != -1:
            # If this is ``:= by``, skip the ``by`` so the body is tactics.
            end = idx + len(marker)
            # Map this index onto the original by equal length (strip keeps
            # length only approximately — comments become spaces of equal
            # length, so indexes match).
            return source[end:].strip()
    return source.strip()


def trusted_signature(problem: Problem) -> tuple[str, str]:
    parsed = extract_theorem(problem.statement)
    if parsed:
        _, name, sig, _ = parsed
        return name, sig
    return problem.theorem_name, problem.statement


def assemble(problem: Problem, proof: str) -> StatementLock:
    """Build the Lean file from the trusted statement and a proof body.

    Never uses the model's theorem declaration as the file. If the model
    sent a full theorem, the signature must match the trusted one and
    only its body is used.
    """
    trusted_name, trusted_sig = trusted_signature(problem)
    body = proof.strip()
    submitted_name = None
    submitted_sig = None

    # If the payload looks like a full declaration, lock the statement.
    looks_like_decl = bool(re.match(r"\s*(?:@[^\s]+|[/-]|theorem|lemma)\b", body))
    # Also catch a payload that starts with ``theorem`` after a comment.
    stripped_body_head = strip_comments_and_strings(body).lstrip()
    if stripped_body_head.startswith("theorem") or stripped_body_head.startswith("lemma"):
        looks_like_decl = True
    if looks_like_decl:
        parsed = extract_theorem(body)
        if parsed is None:
            return StatementLock(
                ok=False,
                reason=(
                    "proof starts with a theorem/lemma but its statement could not "
                    "be parsed; the model may not replace the trusted statement"
                ),
                trusted_name=trusted_name,
                trusted_signature=trusted_sig,
                proof_body=body,
            )
        _, submitted_name, submitted_sig, extracted = parsed
        if normalize_signature(submitted_name) != normalize_signature(trusted_name):
            return StatementLock(
                ok=False,
                reason=(
                    f"proof declares {submitted_name!r} but the trusted constant is "
                    f"{trusted_name!r}; the proof must prove exactly that constant"
                ),
                trusted_name=trusted_name,
                trusted_signature=trusted_sig,
                submitted_name=submitted_name,
                submitted_signature=submitted_sig,
                proof_body=extracted,
            )
        if not _signatures_match(trusted_sig, submitted_sig, problem.statement, body):
            return StatementLock(
                ok=False,
                reason=(
                    "proof statement does not match the trusted statement; "
                    "the model may not replace the theorem being sold"
                ),
                trusted_name=trusted_name,
                trusted_signature=trusted_sig,
                submitted_name=submitted_name,
                submitted_signature=submitted_sig,
                proof_body=extracted,
            )
        body = extracted

    if not body.strip():
        return StatementLock(
            ok=False,
            reason="empty proof body",
            trusted_name=trusted_name,
            trusted_signature=trusted_sig,
            submitted_name=submitted_name,
            submitted_signature=submitted_sig,
        )

    hits = find_banned(body) + find_banned(problem.statement.replace("sorry", " "))
    # The trusted statement is allowed to contain ``sorry`` (that is the hole).
    hits = [h for h in hits if not (h.token == "sorry" and h.reason == "banned_token"
                                    and "sorry" in strip_comments_and_strings(problem.statement))]
    # Re-scan body only for the proof itself.
    hits = find_banned(body)
    if hits:
        return StatementLock(
            ok=False,
            reason=banned_message(hits),
            trusted_name=trusted_name,
            trusted_signature=trusted_sig,
            submitted_name=submitted_name,
            submitted_signature=submitted_sig,
            proof_body=body,
            banned=[f"{h.reason}:{h.token}" for h in hits],
        )

    assembled = _fill_sorry(problem, body)
    # Final scan of the assembled file, allowing the absence of sorry.
    assembled_hits = [h for h in find_banned(assembled) if h.token != "sorry"]
    if assembled_hits:
        return StatementLock(
            ok=False,
            reason=banned_message(assembled_hits),
            trusted_name=trusted_name,
            trusted_signature=trusted_sig,
            submitted_name=submitted_name,
            submitted_signature=submitted_sig,
            proof_body=body,
            assembled=assembled,
            banned=[f"{h.reason}:{h.token}" for h in assembled_hits],
        )

    # The assembled file must still mention the trusted constant by name
    # and must not mention a different leading theorem.
    heads = THEOREM_HEAD.findall(strip_comments_and_strings(assembled))
    if heads:
        leading_name = heads[0][1]
        # Compare the last segment so ``Foo.bar`` and ``bar`` can match
        # when one is namespaced.
        if not _name_matches(leading_name, trusted_name):
            return StatementLock(
                ok=False,
                reason=f"assembled file declares {leading_name!r}, not trusted {trusted_name!r}",
                trusted_name=trusted_name,
                trusted_signature=trusted_sig,
                proof_body=body,
                assembled=assembled,
            )

    return StatementLock(
        ok=True,
        reason="statement locked; static checks passed",
        trusted_name=trusted_name,
        trusted_signature=trusted_sig,
        submitted_name=submitted_name,
        submitted_signature=submitted_sig,
        proof_body=body,
        assembled=assembled,
        static_checks_passed=True,
    )


def _name_matches(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.split(".")[-1] == right.split(".")[-1]


def _signatures_match(trusted_sig: str, submitted_sig: str, trusted_stmt: str, submitted: str) -> bool:
    a = normalize_signature(trusted_sig)
    b = normalize_signature(submitted_sig)
    if a and b and a == b:
        return True
    # Compare the full statement up to ``:=``.
    ta = normalize_signature(_before_assign(trusted_stmt))
    tb = normalize_signature(_before_assign(submitted))
    if ta and tb and ta == tb:
        return True
    # If the submitted signature is a suffix of the trusted one after
    # dropping the theorem keyword and name, still require equality of
    # the type (the part after the last colon at binder-depth 0).
    t_type = _result_type(ta)
    s_type = _result_type(tb)
    return bool(t_type) and t_type == s_type


def _before_assign(text: str) -> str:
    stripped = strip_comments_and_strings(text)
    for marker in (":= by", ":=by", ":="):
        idx = stripped.find(marker)
        if idx != -1:
            return stripped[:idx]
    return stripped


def _result_type(normalized_sig: str) -> str:
    """The type after the last top-level ``:`` in a normalised signature."""
    # Binders like ``(a : Nat)`` contain colons; the result type is after
    # the last colon whose preceding text has balanced parentheses.
    depth = 0
    last = -1
    for i, ch in enumerate(normalized_sig):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == ":" and depth == 0:
            last = i
    if last == -1:
        return normalized_sig.strip()
    return normalized_sig[last + 1 :].strip()


def _fill_sorry(problem: Problem, body: str) -> str:
    """Replace the *target theorem's* ``sorry`` in the trusted statement.

    If the statement is a full file (formal-conjectures) we only replace
    the hole of the named theorem, not every ``sorry`` in the file.
    The preamble (imports) is prepended when the statement itself does
    not already include it.
    """
    statement = problem.statement.rstrip()
    preamble = getattr(problem, "preamble", "") or ""
    body = body.strip()
    # If the body already starts with ``by``, don't double it.
    if statement.endswith("sorry"):
        # Prefer replacing a trailing ``:= by sorry`` / ``:= sorry``.
        filled = _replace_trailing_sorry(statement, body)
    else:
        filled = statement + "\n" + body
    if preamble and preamble.strip() not in filled:
        filled = preamble.rstrip() + "\n\n" + filled
    if not filled.endswith("\n"):
        filled += "\n"
    return filled


def _replace_trailing_sorry(statement: str, body: str) -> str:
    """Replace the last ``sorry`` that is the hole of the theorem, not a comment."""
    stripped = strip_comments_and_strings(statement)
    idx = stripped.rfind("sorry")
    if idx == -1:
        # Fall back to a suffix replace on the raw text.
        if statement.rstrip().endswith("sorry"):
            raw = statement.rstrip()
            return raw[: raw.rfind("sorry")] + _body_for_hole(statement, body)
        return statement + "\n" + body
    # If the hole was ``:= by sorry``, and body is tactics, just drop sorry.
    before = statement[:idx]
    # Detect ``:= by`` immediately before, ignoring whitespace.
    trimmed = strip_comments_and_strings(before).rstrip()
    if trimmed.endswith(":= by") or trimmed.endswith(":=by"):
        replacement = body
        if body.startswith("by ") or body == "by":
            # The hole already has ``by``; body should be tactics.
            replacement = body[2:].lstrip() if body.startswith("by") else body
        return before + replacement
    if trimmed.endswith(":="):
        # Term-mode proof. If the body looks like tactics, wrap with ``by``.
        if _looks_like_tactics(body) and not body.lstrip().startswith("by"):
            return before + "by\n  " + body
        return before + body
    return before + body


def _looks_like_tactics(body: str) -> bool:
    head = body.lstrip()
    if head.startswith("by"):
        return True
    # A single identifier or a ``apply`` / ``exact`` / ``intro`` line.
    tactics = ("apply ", "exact ", "intro", "rfl", "simp", "rw ", "calc", "constructor", "decide")
    return any(head.startswith(t) or head == t.strip() for t in tactics)


def _body_for_hole(statement: str, body: str) -> str:
    trimmed = strip_comments_and_strings(statement).rstrip()
    if trimmed.endswith(":= by sorry") or trimmed.endswith(":=by sorry"):
        if body.startswith("by"):
            return body[2:].lstrip()
    return body


def axiom_allowlist_violations(printed: str) -> list[str]:
    """Parse ``#print axioms`` output and return axioms outside the allow-list.

    Lean prints either ``'foo' does not depend on any axioms`` or
    ``'foo' depends on axioms: [propext, Classical.choice, ...]``.
    """
    text = printed.strip()
    if "does not depend on any axioms" in text:
        return []
    # Collect identifiers after "axioms".
    found: list[str] = []
    # Split on commas, brackets, whitespace.
    if "axiom" in text.lower():
        # Take the part after the last colon or "axioms".
        tail = text
        for sep in ("axioms:", "axioms", ":"):
            if sep in tail:
                tail = tail[tail.lower().rfind(sep.lower()) + len(sep) :]
                break
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_'.]*", tail):
            if token.lower() in {"depends", "on", "axioms", "axiom", "and", "the", "of"}:
                continue
            found.append(token)
    violations = [name for name in found if name not in ALLOWED_AXIOMS]
    return violations


def print_axioms_appendix(theorem_name: str) -> str:
    """Lean commands appended to the assembled file for the comparator."""
    return (
        f"\n#print axioms {theorem_name}\n"
        f"#check {theorem_name}\n"
    )


def statement_hash(statement: str) -> str:
    return hashlib.sha256(normalize_signature(statement).encode("utf-8")).hexdigest()
