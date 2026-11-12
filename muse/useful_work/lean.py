"""Verify a Lean 4 proof. CI uses the mock; a real toolchain is optional.

The mock is conservative: it rejects anything that still contains ``sorry``
(or ``admit``), and it accepts a small set of known-good proofs for the
bundled samples. Unknown proofs are rejected so a hallucinated "proof" does
not get a verified hash.

The real verifier writes a temporary Lean file and runs ``lean <file>``.
It is skipped when the binary is missing. See ``docs/LEAN.md``.
"""

from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from muse.useful_work.problems import Problem

# Known-good completions for bundled samples. The mock accepts these (and
# close whitespace variants) so tests do not depend on a Lean install.
KNOWN_GOOD: dict[str, tuple[str, ...]] = {
    "sample.nat_add_comm": (
        "Nat.add_comm a b",
        "Nat.add_comm _ _",
        "apply Nat.add_comm",
        "exact Nat.add_comm a b",
        "rw [Nat.add_comm]",
        "simp [Nat.add_comm]",
    ),
    "sample.nat_add_zero": (
        "Nat.add_zero a",
        "Nat.add_zero _",
        "rfl",
        "apply Nat.add_zero",
        "exact Nat.add_zero a",
        "rw [Nat.add_zero]",
        "simp",
    ),
}


@dataclass
class VerificationResult:
    ok: bool
    verdict: str  # "verified" | "rejected" | "error"
    verifier: str  # "lean4-mock" | "lean4"
    lean_version: str
    message: str
    proof: str
    problem_id: str

    def as_dict(self) -> dict[str, str | bool]:
        return {
            "ok": self.ok,
            "verdict": self.verdict,
            "verifier": self.verifier,
            "leanVersion": self.lean_version,
            "message": self.message,
            "problemId": self.problem_id,
        }


@dataclass
class LeanVerifier:
    mode: str = "mock"  # "mock" | "real"
    lean_bin: str | None = None
    timeout_seconds: float = 30.0
    known_good: dict[str, tuple[str, ...]] = field(default_factory=lambda: dict(KNOWN_GOOD))

    def verify(self, problem: Problem, proof: str) -> VerificationResult:
        if self.mode == "mock":
            return self._mock(problem, proof)
        if self.mode == "real":
            return self._real(problem, proof)
        raise ValueError(f"unknown lean mode {self.mode!r}")

    def _mock(self, problem: Problem, proof: str) -> VerificationResult:
        cleaned = _tactics(proof)
        if _contains_sorry(proof):
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4-mock",
                lean_version="mock",
                message="proof still contains sorry or admit",
                proof=proof,
                problem_id=problem.problem_id,
            )
        if not cleaned:
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4-mock",
                lean_version="mock",
                message="empty proof",
                proof=proof,
                problem_id=problem.problem_id,
            )
        expected = self.known_good.get(problem.problem_id, ())
        if expected and any(_tactics_match(cleaned, item) for item in expected):
            return VerificationResult(
                ok=True,
                verdict="verified",
                verifier="lean4-mock",
                lean_version="mock",
                message="accepted by the phase 0 mock (known-good tactic for this sample)",
                proof=proof,
                problem_id=problem.problem_id,
            )
        if expected:
            return VerificationResult(
                ok=False,
                verdict="rejected",
                verifier="lean4-mock",
                lean_version="mock",
                message="proof is not in the mock's known-good set for this sample",
                proof=proof,
                problem_id=problem.problem_id,
            )
        return VerificationResult(
            ok=False,
            verdict="rejected",
            verifier="lean4-mock",
            lean_version="mock",
            message="no mock oracle for this problem; run with MUSE_LEAN_MODE=real",
            proof=proof,
            problem_id=problem.problem_id,
        )

    def _real(self, problem: Problem, proof: str) -> VerificationResult:
        binary = self.lean_bin or shutil.which("lean")
        if not binary:
            return VerificationResult(
                ok=False,
                verdict="error",
                verifier="lean4",
                lean_version="unavailable",
                message=(
                    "lean binary not found on PATH; install elan (see docs/LEAN.md) "
                    "or use MUSE_LEAN_MODE=mock"
                ),
                proof=proof,
                problem_id=problem.problem_id,
            )
        version = _lean_version(binary)
        source = _assemble_source(problem, proof)
        with tempfile.TemporaryDirectory(prefix="muse-lean-") as tmp:
            file = Path(tmp) / "Problem.lean"
            file.write_text(source, encoding="utf-8")
            try:
                proc = subprocess.run(  # noqa: S603 - binary is lean from PATH or a setting
                    [binary, str(file)],
                    check=False,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout_seconds,
                    env={**os.environ, "LEAN_PATH": os.environ.get("LEAN_PATH", "")},
                )
            except subprocess.TimeoutExpired:
                return VerificationResult(
                    ok=False,
                    verdict="error",
                    verifier="lean4",
                    lean_version=version,
                    message=f"lean timed out after {self.timeout_seconds}s",
                    proof=proof,
                    problem_id=problem.problem_id,
                )
            except OSError as exc:
                return VerificationResult(
                    ok=False,
                    verdict="error",
                    verifier="lean4",
                    lean_version=version,
                    message=f"lean binary not runnable: {exc}",
                    proof=proof,
                    problem_id=problem.problem_id,
                )
        log = (proc.stdout or "") + (proc.stderr or "")
        if proc.returncode == 0 and not _contains_sorry(proof):
            return VerificationResult(
                ok=True,
                verdict="verified",
                verifier="lean4",
                lean_version=version,
                message=log.strip() or "lean exited 0",
                proof=proof,
                problem_id=problem.problem_id,
            )
        return VerificationResult(
            ok=False,
            verdict="rejected",
            verifier="lean4",
            lean_version=version,
            message=log.strip() or f"lean exited {proc.returncode}",
            proof=proof,
            problem_id=problem.problem_id,
        )


def _contains_sorry(proof: str) -> bool:
    lowered = proof.lower()
    for token in ("sorry", "admit"):
        if token in lowered:
            return True
    return False


def _tactics(proof: str) -> str:
    text = proof.strip()
    marker = ":= by"
    if marker in text:
        text = text.split(marker, 1)[1]
    lines = []
    for line in text.splitlines():
        stripped = line.split("--", 1)[0].strip()
        if stripped:
            lines.append(stripped)
    return " ".join(lines).strip()


def _tactics_match(cleaned: str, expected: str) -> bool:
    left = "".join(cleaned.split()).lower()
    right = "".join(expected.split()).lower()
    return left == right or expected.lower() in cleaned.lower()


def _assemble_source(problem: Problem, proof: str) -> str:
    body = proof.strip()
    if body.startswith("theorem") or body.startswith("lemma"):
        source = body
    else:
        statement = problem.statement.rstrip()
        if statement.endswith("sorry"):
            source = statement[: -len("sorry")] + body
        else:
            source = statement + "\n" + body
    return source + "\n"


def _lean_version(binary: str) -> str:
    try:
        proc = subprocess.run(  # noqa: S603
            [binary, "--version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    text = (proc.stdout or proc.stderr or "unknown").strip()
    return text.splitlines()[0] if text else "unknown"


def statement_hash(statement: str) -> str:
    return hashlib.sha256(statement.encode("utf-8")).hexdigest()
