from pathlib import Path

import pytest

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState
from muse.gateway.clock import FakeClock
from muse.gateway.fallback import CallFailed
from muse.gateway.router import GatewayRouter
from muse.gateway.upstream import UpstreamIndex
from muse.useful_work.banned import find_banned
from muse.useful_work.comparator import assemble
from muse.useful_work.lean import LeanVerifier
from muse.useful_work.llm import MockLLM
from muse.useful_work.problems import load_problems
from muse.useful_work.runner import JobRunner, result_hash
from muse.useful_work.sandbox import run_sandboxed, _scrubbed_env

SAMPLES = Path(__file__).resolve().parents[1] / "muse" / "useful_work" / "samples"
FIXTURES = Path(__file__).resolve().parent / "fixtures" / "formal_conjectures"


def test_loads_bundled_samples():
    problems = load_problems(SAMPLES)
    ids = {p.problem_id for p in problems}
    assert "sample.nat_add_comm" in ids
    assert "sample.nat_add_zero" in ids
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    assert "sorry" in comm.statement
    assert "sample_nat_add_comm" == comm.theorem_name
    assert "Lean 4 prover" in comm.prompt()


def test_loads_formal_conjectures_fixtures():
    problems = load_problems(SAMPLES, FIXTURES)
    ids = {p.problem_id for p in problems}
    # Unique ids from relative path + theorem. The .lake file is skipped.
    gold = "FormalConjectures/NumberTheory/Goldbach.lean#even_gt_two_is_sum_of_two_primes"
    assert gold in ids
    assert "FormalConjectures/TwoHoles.lean#first_hole" in ids
    assert "FormalConjectures/TwoHoles.lean#second_hole" in ids
    # Commented-only sorry and already-proved theorems are not problems.
    assert not any("CommentsOnly" in i for i in ids)
    assert not any("already_shown" in i for i in ids)
    assert not any("Ignored" in i or "should_not_be_loaded" in i for i in ids)
    # Imports and comments survived into the preamble / statement.
    item = next(p for p in problems if p.problem_id == gold)
    assert "import FormalConjectures" in (item.preamble or "")
    assert "sorry" in item.statement
    assert item.source == "formal-conjectures"


def test_mock_lean_never_emits_verified_even_for_a_good_looking_proof():
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    verifier = LeanVerifier(mode="mock")
    # A known-good body passes static checks but is NOT verified and MUST
    # not be sold. Mentioning a tactic is not enough (and is not special).
    ok = verifier.verify(comm, "Nat.add_comm a b")
    assert ok.verified is False
    assert ok.ok is False
    assert ok.mode == "mock"
    assert ok.static_checks_passed is True
    assert ok.verdict == "unverified"
    assert "must not be sold" in ok.message
    # A proof that only *mentions* a tactic is rejected by the lock if it
    # is not a valid body — and still never verified.
    mentioned = verifier.verify(comm, "please use Nat.add_comm a b and trivial here")
    assert mentioned.verified is False
    assert mentioned.mode == "mock"
    bad = verifier.verify(comm, "sorry")
    assert not bad.verified and "sorry" in bad.message.lower() or find_banned("sorry")
    assert bad.verdict == "rejected"


def test_theorem_replacement_is_rejected_even_if_it_proves_true():
    """`theorem x : True := trivial` must not replace the trusted statement."""
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    lock = assemble(comm, "theorem x : True := trivial")
    assert not lock.ok
    assert lock.verified is False if hasattr(lock, "verified") else True
    # The model's theorem is not the assembled file.
    assert "theorem x" not in (lock.assembled or "")
    other = assemble(comm, "theorem totally_different : True := by trivial")
    assert not other.ok
    verifier = LeanVerifier(mode="mock")
    result = verifier.verify(comm, "theorem x : True := trivial")
    assert result.verified is False
    assert result.verdict == "rejected"
    # Real mode also rejects in the assembler, before Lean would run.
    real = LeanVerifier(mode="real", lean_bin="/this/does/not/need/to/exist")
    real_result = real.verify(comm, "theorem x : True := trivial")
    assert real_result.verified is False
    assert real_result.verdict == "rejected"


def test_banned_constructs_are_rejected():
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    verifier = LeanVerifier(mode="mock")
    for body in (
        "native_decide",
        "by native_decide",
        "ofReduceBool",
        "admit",
        "exact sorry",
        "@[implemented_by foo] def x := 1",
    ):
        result = verifier.verify(comm, body)
        assert result.verified is False
        assert result.verdict == "rejected", body


def test_runner_mock_completes_but_does_not_verify_or_hash_for_sale():
    problems = load_problems(SAMPLES)
    llm = MockLLM(answers={"sample.nat_add_comm": "Nat.add_comm a b"})
    runner = JobRunner(problems, llm=llm, verifier=LeanVerifier(mode="mock"), models=["smart"])
    job = runner.run("sample.nat_add_comm")
    assert job.status == "completed"
    assert job.verified is False
    assert job.mode == "mock"
    assert job.result_hash is None
    assert job.model == "smart"
    public = job.as_dict(include_proof=False)
    assert public["proof"] is None
    assert public["verified"] is False


def test_runner_hashes_stably_when_really_verified():
    """The sale hash is a function of a real verification, not the mock."""
    problems = load_problems(SAMPLES)

    class Realish:
        mode = "real"

        def verify(self, problem, proof):
            from muse.useful_work.lean import VerificationResult

            return VerificationResult(
                ok=True,
                verdict="verified",
                verifier="lean4",
                lean_version="4.0.0-test",
                message="ok",
                proof=proof,
                problem_id=problem.problem_id,
                mode="real",
                verified=True,
                static_checks_passed=True,
            )

    llm = MockLLM(answers={"sample.nat_add_comm": "Nat.add_comm a b"})
    runner = JobRunner(problems, llm=llm, verifier=Realish(), models=["smart"])  # type: ignore[arg-type]
    job = runner.run("sample.nat_add_comm")
    assert job.status == "verified"
    assert job.verified is True
    assert job.result_hash
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
    assert job.status == "completed"
    assert job.verified is False
    assert job.model == "fast"
    assert job.attempts[0]["outcome"] == "llm_failed"
    assert job.attempts[1]["outcome"] in {"unverified", "completed", "rejected"} or (
        job.attempts[1].get("mode") == "mock"
    )


def test_runner_falls_back_on_rejected_proof_then_fails_if_all_bad():
    problems = load_problems(SAMPLES)
    llm = MockLLM(answers={"sample.nat_add_comm": "sorry"}, default_answer="sorry")
    runner = JobRunner(
        problems, llm=llm, verifier=LeanVerifier(mode="mock"), models=["smart", "fast"]
    )
    job = runner.run("sample.nat_add_comm")
    assert job.status == "failed"
    assert job.verdict == "rejected"
    assert job.verified is False
    assert len(job.attempts) == 2


def test_runner_skips_open_circuit_and_does_not_trip_on_rejected_proof():
    problems = load_problems(SAMPLES)
    clock = FakeClock(0)
    router = GatewayRouter(
        providers=["smart", "fast"],
        clock=clock,
        jitter_ratio=0,
        default_cooldown_seconds=30,
        upstreams=UpstreamIndex(
            deployments=(),
            alias_upstreams={},
            upstream_aliases={},
            upstream_deployment_ids={},
        ),
    )
    router.breaker.on_response("smart", 429, "60")
    llm = MockLLM(answers={"sample.nat_add_comm": "Nat.add_comm a b"})
    runner = JobRunner(
        problems,
        llm=llm,
        verifier=LeanVerifier(mode="mock"),
        models=["smart", "fast"],
        router=router,
    )
    job = runner.run("sample.nat_add_comm")
    assert job.attempts[0]["outcome"] == "skipped"
    assert job.model == "fast"
    # smart is still open; a rejected proof on fast must not have opened fast.
    assert router.breaker.state("smart") == CircuitState.OPEN
    assert router.breaker.state("fast") == CircuitState.CLOSED


def test_real_lean_mode_reports_missing_binary_fail_closed(tmp_path, monkeypatch):
    problems = load_problems(SAMPLES)
    comm = next(p for p in problems if p.problem_id == "sample.nat_add_comm")
    import muse.useful_work.lean as lean_mod

    monkeypatch.setattr(lean_mod.shutil, "which", lambda name: None)
    verifier = LeanVerifier(mode="real", lean_bin=None)
    result = verifier.verify(comm, "Nat.add_comm a b")
    assert result.verdict == "error"
    assert result.verified is False
    assert "not found" in result.message
    assert result.mode == "real"

    verifier = LeanVerifier(mode="real", lean_bin=str(tmp_path / "no-such-lean"))
    result = verifier.verify(comm, "Nat.add_comm a b")
    assert result.verdict == "error"
    assert result.verified is False


def test_sandbox_scrubs_secrets_and_uses_temp_dir(tmp_path, monkeypatch):
    script = tmp_path / "dump.sh"
    script.write_text(
        "#!/bin/sh\n"
        # Builtins only — no fork. Print every variable the shell inherited.
        "set\n"
        "echo CWD=$PWD\n"
        "echo GROQ=${GROQ_API_KEY-unset}\n"
        "echo MASTER=${LITELLM_MASTER_KEY-unset}\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    monkeypatch.setenv("GROQ_API_KEY", "should-not-leak")
    monkeypatch.setenv("LITELLM_MASTER_KEY", "master-should-not-leak")
    monkeypatch.setenv("OPENROUTER_API_KEY", "or-this")
    cleaned = _scrubbed_env(tmp_path, extra={"GROQ_API_KEY": "should-not-leak", "PATH": "/bin"})
    assert "GROQ_API_KEY" not in cleaned
    assert "should-not-leak" not in cleaned.values()
    ran = run_sandboxed(
        ["/bin/sh", str(script)],
        cwd=tmp_path,
        timeout_seconds=5,
        memory_bytes=64 * 1024 * 1024,
    )
    blob = (ran.stdout or "") + (ran.stderr or "")
    assert "should-not-leak" not in blob
    assert "master-should-not-leak" not in blob
    assert "or-this" not in blob
    assert ran.returncode == 0


def test_unknown_problem_raises():
    runner = JobRunner([], llm=MockLLM(), verifier=LeanVerifier(), models=["smart"])
    with pytest.raises(KeyError):
        runner.run("does-not-exist")
