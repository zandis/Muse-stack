"""Phase 0 HTTP service: provider health, x402 settlement, useful-work jobs.

Default bind is 127.0.0.1:4100, next to the LiteLLM proxy on 4000. The
service does not call mainnet, does not settle real funds, and redacts
secret values from error responses.
"""

from __future__ import annotations

import os
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from muse.config import Settings, load_settings
from muse.gateway.keys import assert_response_safe, key_status
from muse.gateway.router import GatewayRouter
from muse.secrets import redact
from muse.settlement.constants import default_pay_to
from muse.settlement.store import ResultStore
from muse.settlement.verifier import MockVerifier, PaymentRejected
from muse.settlement.x402 import (
    encode_payment_response,
    payment_required,
    read_payment_header,
    usdc_sepolia_option,
)
from muse.useful_work.lean import LeanVerifier
from muse.useful_work.llm import LiteLLMClient, MockLLM
from muse.useful_work.problems import load_problems
from muse.useful_work.runner import JobRunner

DEFAULT_PROVIDERS = [
    "gemini",
    "groq",
    "cohere",
    "openrouter",
    "siliconflow",
    "jina",
    "novita",
    "deepseek",
    "voyage",
    "nvidia_nim",
]


class EventIn(BaseModel):
    provider: str
    status_code: int | None = Field(default=None, alias="statusCode")
    outcome: str = "failed"
    retry_after: str | None = Field(default=None, alias="retryAfter")
    tokens_in: int = Field(default=0, alias="tokensIn")
    tokens_out: int = Field(default=0, alias="tokensOut")
    error: str | None = None

    model_config = {"populate_by_name": True}


class JobIn(BaseModel):
    problem_id: str = Field(alias="problemId")
    models: list[str] | None = None

    model_config = {"populate_by_name": True}


class AppState:
    def __init__(self, settings: Settings, environ: Mapping[str, str]) -> None:
        self.settings = settings
        self.environ = environ
        self.router = GatewayRouter(
            providers=list(DEFAULT_PROVIDERS),
            default_cooldown_seconds=settings.default_cooldown_seconds,
        )
        self.results = ResultStore(settings.data_dir / "results.json")
        self.verifier = MockVerifier(
            network=settings.x402_network,
            asset=settings.x402_usdc,
            pay_to=settings.x402_pay_to or default_pay_to(),
            price_atomic=settings.x402_price_atomic,
        )
        problems = load_problems(settings.problems_dir, settings.formal_conjectures_dir)
        # Prefer a mock LLM unless the caller explicitly asks for the live proxy
        # by setting MUSE_WORK_LLM=litellm. CI and a first boot have no keys.
        llm_mode = environ.get("MUSE_WORK_LLM", "mock").strip().lower() or "mock"
        if llm_mode == "litellm":
            llm = LiteLLMClient(settings.litellm_base_url, master_key=settings.master_key)
        else:
            llm = MockLLM(
                answers={
                    "sample.nat_add_comm": "Nat.add_comm a b",
                    "sample.nat_add_zero": "rfl",
                }
            )
        self.llm_mode = llm_mode
        self.runner = JobRunner(
            problems=problems,
            llm=llm,
            verifier=LeanVerifier(mode=settings.lean_mode),
            router=self.router,
        )
        self._seed_demo_result()

    def _seed_demo_result(self) -> None:
        # A deterministic demo result so the 402 endpoint can be exercised
        # without first running a job. Running a real job adds more records.
        if self.results.get("demo-nat-add-comm"):
            return
        job = self.runner.run("sample.nat_add_comm")
        if job.status == "verified" and job.record and job.proof and job.result_hash:
            self.results.add(
                result_hash=job.result_hash,
                problem_id=job.problem_id,
                title=job.title or job.problem_id,
                verdict=job.verdict or "verified",
                verifier=job.verifier or "lean4-mock",
                model=job.model or "smart",
                proof=job.proof,
                statement=job.statement or "",
                price_atomic=self.settings.x402_price_atomic,
                metadata=job.record,
                result_id="demo-nat-add-comm",
            )


def create_app(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    env = dict(environ if environ is not None else os.environ)
    cfg = settings or load_settings(env)

    app = FastAPI(
        title="Muse Stack phase 0",
        version="0.1.0",
        description=(
            "Routing health, x402 mock settlement on Base Sepolia, and a Lean "
            "useful-work runner. Mock verifier only; no real funds; no mainnet."
        ),
    )
    # Set eagerly so FastAPI TestClient works without a lifespan context.
    app.state.muse = AppState(cfg, env)

    @app.middleware("http")
    async def _auth_and_redact(request: Request, call_next):  # type: ignore[no-untyped-def]
        state: AppState | None = getattr(request.app.state, "muse", None)
        master = cfg.master_key
        path = request.url.path
        if master and path not in {"/health", "/docs", "/openapi.json", "/redoc"}:
            header = request.headers.get("authorization", "")
            expected = f"Bearer {master}"
            if header != expected:
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            response = await call_next(request)
        except Exception as exc:  # noqa: BLE001
            message = redact(str(exc), env)
            return JSONResponse({"error": message, "type": type(exc).__name__}, status_code=500)
        return response

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {
            "ok": True,
            "service": "muse-stack-phase0",
            "network": cfg.x402_network,
            "facilitator": "mock",
            "leanMode": cfg.lean_mode,
            "x402": "stub",
        }

    @app.get("/v1/providers/health")
    def providers_health(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        body = state.router.health.health()
        assert_response_safe(body, env)
        return body

    @app.get("/v1/providers/usage")
    def providers_usage(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        body = state.router.health.usage()
        assert_response_safe(body, env)
        return body

    @app.get("/v1/providers/keys")
    def providers_keys() -> dict[str, Any]:
        body = {
            "keys": key_status(env),
            "note": (
                "Values are never returned. Members add keys by copying "
                ".env.example to .env and restarting. Empty values are omitted "
                "from the LiteLLM catalog."
            ),
        }
        assert_response_safe(body, env)
        return body

    @app.post("/v1/providers/events")
    def providers_events(request: Request, event: EventIn) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        state.router.report_event(
            event.provider,
            status_code=event.status_code,
            outcome=event.outcome,
            retry_after=event.retry_after,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            error=event.error,
        )
        return {"ok": True, "provider": event.provider}

    @app.get("/v1/work/problems")
    def work_problems(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        return {"problems": state.runner.list_problems()}

    @app.post("/v1/work/jobs")
    def work_jobs(request: Request, body: JobIn) -> JSONResponse:
        state: AppState = request.app.state.muse
        try:
            job = state.runner.run(body.problem_id, models=body.models)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        if job.status == "verified" and job.result_hash and job.proof:
            record = state.results.add(
                result_hash=job.result_hash,
                problem_id=job.problem_id,
                title=job.title or job.problem_id,
                verdict=job.verdict or "verified",
                verifier=job.verifier or state.runner.verifier.mode,
                model=job.model or "",
                proof=job.proof,
                statement=job.statement or "",
                price_atomic=cfg.x402_price_atomic,
                metadata=job.record or {},
                result_id=job.job_id,
            )
            payload = job.as_dict()
            payload["listedForSale"] = record.result_id
            return JSONResponse(payload, status_code=201)
        return JSONResponse(job.as_dict(), status_code=200)

    @app.get("/v1/work/jobs/{job_id}")
    def work_job(request: Request, job_id: str) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        job = state.runner.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        return job.as_dict()

    def _resource_url(request: Request, result_id: str) -> str:
        return str(request.base_url).rstrip("/") + f"/v1/results/{result_id}"

    @app.get("/v1/results")
    def list_results(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        return {
            "results": [item.public_view(include_proof=False) for item in state.results.list()],
            "note": "GET a result without a payment to receive HTTP 402; retry with PAYMENT-SIGNATURE.",
        }

    @app.get("/v1/results/{result_id}")
    def get_result(request: Request, result_id: str) -> JSONResponse:
        state: AppState = request.app.state.muse
        record = state.results.get(result_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown result")
        payload = read_payment_header(request.headers)
        option = usdc_sepolia_option(
            amount_atomic=record.price_atomic,
            pay_to=cfg.x402_pay_to,
            asset=cfg.x402_usdc,
            max_timeout_seconds=cfg.x402_max_timeout_seconds,
        )
        if payload is None:
            body, headers = payment_required(
                option=option,
                resource_url=_resource_url(request, result_id),
                description=f"Verified Lean result {record.result_hash[:16]}… ({record.problem_id})",
            )
            preview = record.public_view(include_proof=False)
            body["result"] = preview
            return JSONResponse(body, status_code=402, headers=headers)
        try:
            verification = state.verifier.verify(payload, price_atomic=record.price_atomic)
        except PaymentRejected as exc:
            body, headers = payment_required(
                option=option,
                resource_url=_resource_url(request, result_id),
                description=f"Verified Lean result {record.result_hash[:16]}… ({record.problem_id})",
                error=exc.reason,
            )
            body["rejection"] = {"code": exc.code, "reason": exc.reason}
            return JSONResponse(body, status_code=402, headers=headers)
        sold = state.results.mark_sold(result_id, verification.receipt_id)
        content = sold.public_view(include_proof=True)
        content["receipt"] = verification.as_dict()
        headers = encode_payment_response(verification.as_dict())
        assert_response_safe(content, env)
        return JSONResponse(content, status_code=200, headers=headers)

    return app


def main() -> None:
    import argparse

    import uvicorn

    parser = argparse.ArgumentParser(description="Run the Muse Stack phase 0 service.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    args = parser.parse_args()
    settings = load_settings()
    host = args.host or settings.host
    port = args.port or settings.port
    uvicorn.run("muse.server:create_app", factory=True, host=host, port=port, log_level="info")


if __name__ == "__main__":
    main()
