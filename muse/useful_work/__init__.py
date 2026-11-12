"""Useful-work skeleton: ask routed LLMs for Lean proofs, verify, hash the result.

Mock Lean never emits a verified result and those results are never sold.
Real Lean runs in a sandbox against a trusted statement (lake-comparator
style). The job API does not return the proof.
"""

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
