"""Useful-work skeleton: ask routed LLMs for Lean proofs, verify, hash the result."""

from muse.useful_work.lean import LeanVerifier, VerificationResult
from muse.useful_work.llm import LLMClient, MockLLM
from muse.useful_work.problems import Problem, load_problems
from muse.useful_work.runner import JobRecord, JobRunner

__all__ = [
    "JobRecord",
    "JobRunner",
    "LLMClient",
    "LeanVerifier",
    "MockLLM",
    "Problem",
    "VerificationResult",
    "load_problems",
]
