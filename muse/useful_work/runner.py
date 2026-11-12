"""Run a useful-work job: pick a problem, ask routed models, verify, hash.

The result hash is a SHA-256 over a canonical JSON record of the problem,
the proof, the verdict, and the verifier identity. That hash is what a later
phase would put on chain; the proof itself stays off-chain and can be sold
through the x402 settlement stub.

Models are LiteLLM aliases (``smart``, ``fast``, ``cheap``) by default. The
runner walks them in order. A 429 or a rejected proof moves on to the next
alias; only a provider-level 429 is reported to the optional gateway health
store, so a bad proof does not open a circuit.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from muse.gateway.fallback import CallFailed
from muse.gateway.router import GatewayRouter
from muse.useful_work.lean import LeanVerifier, VerificationResult, statement_hash
from muse.useful_work.llm import LLMClient
from muse.useful_work.problems import Problem, get_problem


def result_hash(record: dict[str, Any]) -> str:
    """Stable SHA-256 of a result record. Keys are sorted; no whitespace drift."""
    canonical = json.dumps(record, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass
class JobRecord:
    job_id: str
    problem_id: str
    status: str  # "pending" | "verified" | "failed"
    result_hash: str | None
    verdict: str | None
    verifier: str | None
    model: str | None
    proof: str | None
    statement: str | None
    title: str | None
    message: str
    attempts: list[dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    finished_at: str | None = None
    record: dict[str, Any] | None = None

    def as_dict(self, *, include_proof: bool = True) -> dict[str, Any]:
        body = {
            "jobId": self.job_id,
            "problemId": self.problem_id,
            "title": self.title,
            "status": self.status,
            "resultHash": self.result_hash,
            "verdict": self.verdict,
            "verifier": self.verifier,
            "model": self.model,
            "message": self.message,
            "attempts": self.attempts,
            "createdAt": self.created_at,
            "finishedAt": self.finished_at,
        }
        if include_proof:
            body["proof"] = self.proof
            body["statement"] = self.statement
            body["record"] = self.record
        return body


@dataclass
class JobRunner:
    problems: list[Problem]
    llm: LLMClient
    verifier: LeanVerifier
    models: Sequence[str] = ("smart", "fast", "cheap")
    router: GatewayRouter | None = None

    def __post_init__(self) -> None:
        self._jobs: dict[str, JobRecord] = {}
        self._lock = threading.Lock()

    def list_problems(self) -> list[dict[str, str]]:
        return [
            {
                "problemId": p.problem_id,
                "title": p.title,
                "theorem": p.theorem_name,
                "source": p.source,
                "path": str(p.path),
            }
            for p in self.problems
        ]

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._jobs.values())

    def run(self, problem_id: str, *, models: Sequence[str] | None = None) -> JobRecord:
        problem = get_problem(self.problems, problem_id)
        job = JobRecord(
            job_id=uuid4().hex,
            problem_id=problem.problem_id,
            status="pending",
            result_hash=None,
            verdict=None,
            verifier=None,
            model=None,
            proof=None,
            statement=problem.statement,
            title=problem.title,
            message="running",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        with self._lock:
            self._jobs[job.job_id] = job
        chain = list(models) if models is not None else list(self.models)
        prompt = problem.prompt()
        attempts: list[dict[str, Any]] = []
        last_verification: VerificationResult | None = None

        for model in chain:
            try:
                proof = self.llm.complete(model=model, prompt=prompt, problem=problem)
            except CallFailed as exc:
                attempts.append(
                    {
                        "model": model,
                        "outcome": "llm_failed",
                        "detail": str(exc),
                        "statusCode": exc.status_code,
                    }
                )
                if self.router is not None and exc.status_code is not None:
                    self.router.report_event(
                        model,
                        status_code=exc.status_code,
                        outcome="failed",
                        retry_after=exc.retry_after,
                        error=str(exc),
                    )
                continue
            verification = self.verifier.verify(problem, proof)
            last_verification = verification
            if not verification.ok:
                attempts.append(
                    {
                        "model": model,
                        "outcome": "rejected",
                        "detail": verification.message,
                        "verdict": verification.verdict,
                    }
                )
                continue
            payload = {
                "problem_id": problem.problem_id,
                "statement_hash": statement_hash(problem.statement),
                "proof_hash": hashlib.sha256(proof.encode("utf-8")).hexdigest(),
                "verdict": verification.verdict,
                "verifier": verification.verifier,
                "lean_version": verification.lean_version,
                "model": model,
            }
            digest = result_hash(payload)
            job.status = "verified"
            job.verdict = verification.verdict
            job.verifier = verification.verifier
            job.model = model
            job.proof = proof
            job.result_hash = digest
            job.message = verification.message
            job.attempts = attempts + [{"model": model, "outcome": "verified"}]
            job.record = {**payload, "result_hash": digest}
            job.finished_at = datetime.now(timezone.utc).isoformat()
            if self.router is not None:
                self.router.report_event(model, status_code=200, outcome="success")
            return job

        job.status = "failed"
        job.attempts = attempts
        if last_verification is not None:
            job.verdict = last_verification.verdict
            job.verifier = last_verification.verifier
            job.proof = last_verification.proof
            job.message = last_verification.message
        else:
            job.message = f"every model in the chain failed: {', '.join(chain)}"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return job
