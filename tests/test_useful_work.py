from pathlib import Path

import pytest

from muse.gateway.fallback import CallFailed
from muse.useful_work.lean import LeanVerifier
from muse.useful_work.llm import MockLLM
from muse.useful_work.problems import load_problems
from muse.useful_work.runner import JobRunner, result_hash

SAMPLES = Path(__file__).resolve().parents[1] / "muse" / "useful_work" / "samples"


def test_loads_bundled_samples():
    problems = load_problems(SAMPLES)
    ids = {p.problem_id for p in problems}
    assert "sample.nat_add_comm" in ids
    assert "sample.nat_add_zero" in ids
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    assert "sorry" in comm.statement
    assert "sample_nat_add_comm" == comm.theorem_name
    assert "Lean 4 prover" in comm.prompt()


def test_mock_lean_accepts_known_good_and_rejects_sorry():
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    verifier = LeanVerifier(mode="mock")
    ok = verifier.verify(comm, "Nat.add_comm a b")
    assert ok.ok and ok.verdict == "verified" and ok.verifier == "lean4-mock"
    bad = verifier.verify(comm, "sorry")
    assert not bad.ok and "sorry" in bad.message
    unknown = verifier.verify(comm, "this is not a proof")
    assert not unknown.ok


def test_runner_verifies_and_hashes_stably():
    problems = load_problems(SAMPLES)
    llm = MockLLM(answers={"sample.nat_add_comm": "exact Nat.add_comm a b"})
    runner = JobRunner(problems, llm=llm, verifier=LeanVerifier(mode="mock"), models=["smart"])
    job = runner.run("sample.nat_add_comm")
    assert job.status == "verified"
    assert job.result_hash
    assert job.record is not None
    # Hash is a function of the canonical record without the hash itself.
    payload = {k: v for k, v in job.record.items() if k != "result_hash"}
    assert result_hash(payload) == job.result_hash
    again = runner.run("sample.nat_add_comm")
    assert again.result_hash == job.result_hash


def test_runner_falls_back_to_next_model_on_llm_failure():
    problems = load_problems(SAMPLES)
    llm = MockLLM(
        answers={"sample.nat_add_comm": "Nat.add_comm a b"},
        fail_models={"smart"},
    )
    runner = JobRunner(
        problems, llm=llm, verifier=LeanVerifier(mode="mock"), models=["smart", "fast"]
    )
    job = runner.run("sample.nat_add_comm")
    assert job.status == "verified"
    assert job.model == "fast"
    assert job.attempts[0]["outcome"] == "llm_failed"
    assert job.attempts[1]["outcome"] == "verified"


def test_runner_falls_back_on_rejected_proof_then_fails_if_all_bad():
    problems = load_problems(SAMPLES)
    llm = MockLLM(answers={"sample.nat_add_comm": "sorry"}, default_answer="sorry")
    runner = JobRunner(
        problems, llm=llm, verifier=LeanVerifier(mode="mock"), models=["smart", "fast"]
    )
    job = runner.run("sample.nat_add_comm")
    assert job.status == "failed"
    assert job.verdict == "rejected"
    assert len(job.attempts) == 2


def test_real_lean_mode_reports_missing_binary(tmp_path, monkeypatch):
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    import muse.useful_work.lean as lean_mod

    monkeypatch.setattr(lean_mod.shutil, "which", lambda name: None)
    verifier = LeanVerifier(mode="real", lean_bin=None)
    result = verifier.verify(comm, "rfl")
    assert result.verdict == "error"
    assert "not found" in result.message

    # An explicit path that does not exist is also an error, not a crash.
    verifier = LeanVerifier(mode="real", lean_bin=str(tmp_path / "no-such-lean"))
    result = verifier.verify(comm, "rfl")
    assert result.verdict == "error"


def test_unknown_problem_raises():
    runner = JobRunner([], llm=MockLLM(), verifier=LeanVerifier(), models=["smart"])
    with pytest.raises(KeyError):
        runner.run("does-not-exist")
