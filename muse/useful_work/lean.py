"""Verify a Lean 4 proof against a trusted statement.

The mock never emits a verified result (``verified: false, mode: mock``)
and mock results are never listed for sale. It still runs the fail-closed
static checks (statement lock, banned constructs) so CI exercises the
same gates the real verifier uses.

The real verifier:

1. Locks the statement (``muse.useful_work.comparator``) — the model's
   text cannot replace the theorem.
2. Rejects banned constructs (``sorry``, ``admit``, ``native_decide``,
   ``ofReduceBool``, ``implemented_by``, ``debug.skipKernelTC``,
   ``extern``, ``axiom``, elaboration-time IO, …).
3. Runs Lean in a secret-free, no-network sandbox (bubblewrap when
   available; rlimits always). See ``docs/LEAN.md`` for the production
   path (gVisor / Firecracker / bubblewrap).
4. Parses ``#print axioms`` and rejects anything outside
   ``propext`` / ``Quot.sound`` / ``Classical.choice``.
5. If the Lean comparator is unavailable, does **not** emit
   ``verdict: verified`` (fail-closed).
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path

from muse.useful_work.comparator import (
    ALLOWED_AXIOMS,
    StatementLock,
    assemble,
    axiom_allowlist_violations,
    print_axioms_appendix,
    statement_hash,
    trusted_signature,
)
from muse.useful_work.problems import Problem
from muse.useful_work.sandbox import run_sandboxed


@dataclass
class VerificationResult:
    ok: bool
    verdict: str  # "verified" | "rejected" | "error" | "unverified"
    verifier: str  # "lean4-mock" | "lean4" | "lean4-unavailable"
    lean_version: str
    message: str
    proof: str
    problem_id: str
    mode: str = "mock"
    verified: bool = False
    static_checks_passed: bool = False
    axioms: list[str] = field(default_factory=list)
    assembled: str = ""

    def as_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "verified": self.verified,
            "verdict": self.verdict,
            "verifier": self.verifier,
            "mode": self.mode,
            "leanVersion": self.lean_version,
            "message": self.message,
            "problemId": self.problem_id,
            "staticChecksPassed": self.static_checks_passed,
            "axioms": list(self.axioms),
        }


@dataclass
class LeanVerifier:
    mode: str = "mock"  # "mock" | "real"
    lean_bin: str | None = None
    timeout_seconds: float = 30.0
    memory_bytes: int = 1 << 30

    def verify(self, problem: Problem, proof: str) -> VerificationResult:
        lock = assemble(problem, proof)
        if self.mode == "mock":
            return self._mock(problem, proof, lock)
        if self.mode == "real":
            return self._real(problem, proof, lock)
        raise ValueError(f"unknown lean mode {self.mode!r}")

    def _base(
        self,
        problem: Problem,
        proof: str,
        lock: StatementLock,
        *,
        mode: str,
        verifier: str,
        lean_version: str,
    ) -> VerificationResult:
        return VerificationResult(
            ok=False,
            verdict="rejected" if not lock.ok else "unverified",
            verifier=verifier,
            lean_version=lean_version,
            message=lock.reason,
            proof=proof,
            problem_id=problem.problem_id,
            mode=mode,
            verified=False,
            static_checks_passed=lock.static_checks_passed,
            assembled=lock.assembled,
        )

    def _mock(self, problem: Problem, proof: str, lock: StatementLock) -> VerificationResult:
        # Mock mode is fail-closed: it never reports verified, and it
        # never accepts a proof merely because a known-good tactic
        # string is mentioned in the text.
        result = self._base(
            problem, proof, lock, mode="mock", verifier="lean4-mock", lean_version="mock"
        )
        if lock.ok:
            result.verdict = "unverified"
            result.message = (
                "static checks passed; mock mode does not verify and "
                "must not be sold (verified: false, mode: mock)"
            )
        return result

    def _real(self, problem: Problem, proof: str, lock: StatementLock) -> VerificationResult:
        if not lock.ok:
            return self._base(
                problem, proof, lock, mode="real", verifier="lean4", lean_version="unavailable"
            )
        binary = self.lean_bin or shutil.which("lean")
        if not binary or not Path(binary).is_file():
            # Fail-closed: without Lean we will not emit verified.
            # An explicit path that does not exist is the same as "not found".
            result = self._base(
                problem,
                proof,
                lock,
                mode="real",
                verifier="lean4-unavailable",
                lean_version="unavailable",
            )
            result.verdict = "error"
            result.message = (
                "lean binary not found on PATH; the fail-closed Python "
                "comparator passed static checks but cannot emit verified "
                "without a real Lean kernel. Install elan (see docs/LEAN.md) "
                "or use MUSE_LEAN_MODE=mock (which also never emits verified)."
            )
            return result

        version = self._lean_version(binary)
        trusted_name, _ = trusted_signature(problem)
        source = lock.assembled + print_axioms_appendix(trusted_name)
        from muse.useful_work.sandbox import make_work_dir

        with make_work_dir() as tmp:
            work = Path(tmp)
            file = work / "Problem.lean"
            file.write_text(source, encoding="utf-8")
            ran = run_sandboxed(
                [binary, str(file)],
                cwd=work,
                timeout_seconds=self.timeout_seconds,
                memory_bytes=self.memory_bytes,
                extra_env=self._lean_env(binary),
            )
        log = (ran.stdout or "") + "\n" + (ran.stderr or "")
        if ran.timed_out:
            return VerificationResult(
                ok=False,
                verdict="error",
                verifier="lean4",
                lean_version=version,
                message=f"lean timed out after {self.timeout_seconds}s (sandboxed, bwrap={ran.used_bwrap})",
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=False,
                static_checks_passed=True,
                assembled=lock.assembled,
            )
        if ran.returncode != 0:
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4",
                lean_version=version,
                message=(log.strip() or f"lean exited {ran.returncode}")
                + f" (sandboxed, bwrap={ran.used_bwrap})",
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=False,
                static_checks_passed=True,
                assembled=lock.assembled,
            )
        violations = axiom_allowlist_violations(ran.stdout or "")
        # If #print axioms produced nothing we can parse, fail closed.
        if not _axioms_section_present(ran.stdout or "") and not _axioms_section_present(ran.stderr or ""):
            return VerificationResult(
                ok=False,
                verdict="error",
                verifier="lean4",
                lean_version=version,
                message=(
                    "lean exited 0 but #print axioms produced no recognisable "
                    "output; fail-closed, not emitting verified. log: "
                    + log.strip()[:500]
                ),
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=False,
                static_checks_passed=True,
                assembled=lock.assembled,
            )
        if violations:
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4",
                lean_version=version,
                message=(
                    "axioms outside the allow-list "
                    f"(allowed: {sorted(ALLOWED_AXIOMS)}): {violations}"
                ),
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=False,
                static_checks_passed=True,
                axioms=violations,
                assembled=lock.assembled,
            )
        # Type / constant presence: #check should mention the trusted name.
        check_out = ran.stdout or ""
        if trusted_name.split(".")[-1] not in check_out and trusted_name not in check_out:
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4",
                lean_version=version,
                message=(
                    f"lean exited 0 but #check did not mention trusted constant "
                    f"{trusted_name!r}; fail-closed"
                ),
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=False,
                static_checks_passed=True,
                assembled=lock.assembled,
            )
        axioms_used = _listed_axioms(ran.stdout or "")
        return VerificationResult(
            ok=True,
            verdict="verified",
            verifier="lean4",
            lean_version=version,
            message=(log.strip() or "lean exited 0") + f" (sandboxed, bwrap={ran.used_bwrap})",
            proof=proof,
            problem_id=problem.problem_id,
            mode="real",
            verified=True,
            static_checks_passed=True,
            axioms=axioms_used,
            assembled=lock.assembled,
        )

    def _lean_env(self, binary: str) -> dict[str, str]:
        # Only toolchain paths, never the parent environment.
        extra: dict[str, str] = {}
        path = Path(binary).resolve()
        extra["PATH"] = str(path.parent) + ":/usr/local/bin:/usr/bin:/bin"
        # If this looks like an elan shim, let Lean find the toolchain
        # through a read-only bind (sandbox.py binds ~/.elan) and HOME
        # inside the work dir — we do not forward ELAN_HOME from the
        # parent if we cannot confirm it is a path (not a secret).
        elan = path.parent.parent  # e.g. ~/.elan/bin/lean → ~/.elan
        if (elan / "toolchains").is_dir():
            extra["ELAN_HOME"] = str(elan)
        return extra

    def _lean_version(self, binary: str) -> str:
        from muse.useful_work.sandbox import make_work_dir

        try:
            with make_work_dir(prefix="muse-lean-ver-") as tmp:
                ran = run_sandboxed(
                    [binary, "--version"],
                    cwd=Path(tmp),
                    timeout_seconds=10,
                    extra_env=self._lean_env(binary),
                )
        except OSError:
            return "unknown"
        text = (ran.stdout or ran.stderr or "unknown").strip()
        return text.splitlines()[0] if text else "unknown"


def _axioms_section_present(text: str) -> bool:
    lower = text.lower()
    return "axiom" in lower  # covers "does not depend on any axioms" too


def _listed_axioms(text: str) -> list[str]:
    if "does not depend on any axioms" in text:
        return []
    violations_or_all = axiom_allowlist_violations(text)
    # The helper returns only violations; re-parse for the allowed ones too.
    from muse.useful_work.comparator import ALLOWED_AXIOMS
    import re

    found = re.findall(r"[A-Za-z][A-Za-z0-9_'.]*", text)
    return [t for t in found if t in ALLOWED_AXIOMS or t in violations_or_all]


# Re-export so existing imports of statement_hash keep working.
__all__ = [
    "LeanVerifier",
    "VerificationResult",
    "statement_hash",
    "assemble",
    "StatementLock",
]
