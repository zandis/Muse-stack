"""Run a useful-work job: pick a problem, ask routed models, verify, hash.

The result hash is a SHA-256 over a canonical JSON record of the problem,
the proof, the verdict, and the verifier identity. That hash is what a
later phase would put on chain; the proof itself stays off-chain and is
sold only through the x402 settlement stub.

Models are LiteLLM aliases (``smart``, ``fast``, ``cheap``) by default.
The runner walks them in order, *through the circuit breaker*: an open
circuit is skipped, a 429 is reported under the upstream model id (so
sibling aliases share the cooldown), and a rejected proof moves on
without opening a circuit.

Mock Lean results are never marked ``verified`` and are never listed
for sale. The job API never returns the proof (that is the paid good).
Problem listings never include local filesystem paths.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Sequence
from uuid import uuid4

from muse.gateway.circuit_breaker import CircuitOpen
from muse.gateway.fallback import CallFailed
from muse.gateway.quota import is_quota_exhausted
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
    status: str  # "pending" | "verified" | "failed" | "completed"
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
    mode: str = "mock"
    verified: bool = False

    def as_dict(self, *, include_proof: bool = False) -> dict[str, Any]:
        """Public view. The proof is withheld unless explicitly requested
        (and the HTTP layer must not request it — that is the paid good).
        """
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
            "mode": self.mode,
            "verified": self.verified,
        }
        if include_proof:
            body["proof"] = self.proof
            body["statement"] = self.statement
            body["record"] = self.record
        else:
            body["statement"] = self.statement  # the statement is the quote, not the goods
            body["proof"] = None
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
        # Deliberately no filesystem ``path`` — that leaked a local location.
        return [
            {
                "problemId": p.problem_id,
                "title": p.title,
                "theorem": p.theorem_name,
                "source": p.source,
            }
            for p in self.problems
        ]

    def get(self, job_id: str) -> JobRecord | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list_jobs(self) -> list[JobRecord]:
        with self._lock:
            return list(self._jobs.values())

    def _breaker_key(self, model: str, upstream: str | None = None) -> str:
        if upstream:
            return upstream
        if self.router is not None and self.router.upstreams is not None:
            return self.router.upstreams.breaker_key(model)
        return model

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
            mode=self.verifier.mode,
            verified=False,
        )
        with self._lock:
            self._jobs[job.job_id] = job
        chain = list(models) if models is not None else list(self.models)
        prompt = problem.prompt()
        attempts: list[dict[str, Any]] = []
        last_verification: VerificationResult | None = None

        for model in chain:
            ticket = None
            if self.router is not None:
                try:
                    ticket = self.router.breaker.allow(model)
                except CircuitOpen as exc:
                    attempts.append(
                        {
                            "model": model,
                            "outcome": "skipped",
                            "detail": str(exc),
                            "statusCode": 429,
                        }
                    )
                    self.router.report_event(
                        model, status_code=429, outcome="skipped", error=str(exc)
                    )
                    continue
            try:
                proof = self.llm.complete(model=model, prompt=prompt, problem=problem)
            except CallFailed as exc:
                attempts.append(
                    {
                        "model": model,
                        "outcome": "llm_failed",
                        "detail": str(exc),
                        "statusCode": exc.status_code,
                        "upstream": exc.upstream,
                    }
                )
                if self.router is not None:
                    exhausted = exc.quota_exhausted or is_quota_exhausted(
                        status_code=exc.status_code, message=str(exc)
                    )
                    key = self._breaker_key(model, exc.upstream)
                    self.router.report_event(
                        model,
                        status_code=exc.status_code,
                        outcome="failed",
                        retry_after=exc.retry_after,
                        error=str(exc),
                        quota_exhausted=exhausted,
                        upstream=key,
                    )
                continue
            verification = self.verifier.verify(problem, proof)
            last_verification = verification
            if not verification.verified:
                attempts.append(
                    {
                        "model": model,
                        "outcome": "rejected" if verification.verdict == "rejected" else verification.verdict,
                        "detail": verification.message,
                        "verdict": verification.verdict,
                        "mode": verification.mode,
                        "verified": False,
                    }
                )
                # A rejected proof is not a provider failure.
                # In mock mode a static-pass is as far as we go: complete
                # the job, do not keep spending models, do not mark verified
                # or list for sale.
                if verification.mode == "mock" and verification.static_checks_passed:
                    job.status = "completed"
                    job.verified = False
                    job.verdict = verification.verdict
                    job.verifier = verification.verifier
                    job.mode = verification.mode
                    job.model = model
                    job.proof = proof
                    job.message = verification.message
                    job.attempts = attempts
                    job.finished_at = datetime.now(timezone.utc).isoformat()
                    if self.router is not None:
                        self.router.report_event(model, status_code=200, outcome="success")
                    return job
                continue
            payload = {
                "problem_id": problem.problem_id,
                "statement_hash": statement_hash(problem.statement),
                "proof_hash": hashlib.sha256(proof.encode("utf-8")).hexdigest(),
                "verdict": verification.verdict,
                "verifier": verification.verifier,
                "lean_version": verification.lean_version,
                "model": model,
                "mode": verification.mode,
            }
            digest = result_hash(payload)
            job.status = "verified"
            job.verified = True
            job.verdict = verification.verdict
            job.verifier = verification.verifier
            job.mode = verification.mode
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

        job.status = "failed" if (last_verification is None or last_verification.verdict == "rejected") else "completed"
        # Mock-mode static-pass is "completed" / unverified, never "verified".
        if last_verification is not None and last_verification.mode == "mock" and last_verification.static_checks_passed:
            job.status = "completed"
        job.attempts = attempts
        job.verified = False
        if last_verification is not None:
            job.verdict = last_verification.verdict
            job.verifier = last_verification.verifier
            job.mode = last_verification.mode
            job.proof = last_verification.proof
            job.message = last_verification.message
        else:
            job.message = f"every model in the chain failed or was skipped: {', '.join(chain)}"
        job.finished_at = datetime.now(timezone.utc).isoformat()
        return job
