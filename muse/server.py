"""Phase 0 HTTP service: provider health, x402 settlement, useful-work jobs.

Default bind is 127.0.0.1:4100, next to the LiteLLM proxy on 4000. The
service does not call mainnet, does not settle real funds, and redacts
secret values from error responses.

Startup refuses a missing master key unless the bind is loopback, in
which case it logs a loud warning. When a master key is set, every
endpoint (including /health and /docs) requires it. The job API never
returns a paid proof. App construction does not run a job. Sales are
recorded only after the response has been built and is being delivered.
"""

from __future__ import annotations

import hmac
import logging
import os
import threading
import time
from typing import Any, Mapping

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from starlette.background import BackgroundTask

from muse.config import Settings, load_settings
from muse.gateway.keys import assert_response_safe, key_status
from muse.gateway.retry_after import InvalidRetryAfter, parse_retry_after
from muse.gateway.router import GatewayRouter
from muse.secrets import redact
from muse.settlement.constants import default_pay_to
from muse.settlement.quotes import QuoteStore
from muse.settlement.store import ResultStore
from muse.settlement.verifier import MockVerifier, PaymentRejected
from muse.settlement.x402 import (
    InvalidPaymentHeader,
    encode_payment_response,
    payment_required,
    read_payment_header,
    usdc_sepolia_option,
)
from muse.useful_work.lean import LeanVerifier
from muse.useful_work.llm import LiteLLMClient, MockLLM
from muse.useful_work.problems import load_problems
from muse.useful_work.runner import JobRunner

log = logging.getLogger("muse.server")

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

LOOPBACK = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


class EventIn(BaseModel):
    provider: str
    status_code: int | None = Field(default=None, alias="statusCode")
    outcome: str = "failed"
    retry_after: str | None = Field(default=None, alias="retryAfter")
    tokens_in: int = Field(default=0, alias="tokensIn")
    tokens_out: int = Field(default=0, alias="tokensOut")
    error: str | None = None
    quota_exhausted: bool = Field(default=False, alias="quotaExhausted")
    upstream: str | None = None

    model_config = {"populate_by_name": True}


class JobIn(BaseModel):
    problem_id: str = Field(alias="problemId")
    models: list[str] | None = None

    model_config = {"populate_by_name": True}


class JobLimiter:
    """In-process concurrency cap and optional spacing for useful-work jobs."""

    def __init__(self, max_concurrent: int = 2, min_interval_seconds: float = 0.0) -> None:
        self._sema = threading.BoundedSemaphore(max(1, max_concurrent))
        self._min_interval = min_interval_seconds
        self._lock = threading.Lock()
        self._last = 0.0

    def acquire(self, timeout: float = 0.05) -> bool:
        if not self._sema.acquire(timeout=timeout):
            return False
        if self._min_interval > 0:
            with self._lock:
                wait = self._min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            with self._lock:
                self._last = time.monotonic()
        return True

    def release(self) -> None:
        try:
            self._sema.release()
        except ValueError:
            pass


class AppState:
    def __init__(self, settings: Settings, environ: Mapping[str, str]) -> None:
        self.settings = settings
        self.environ = environ
        self.router = GatewayRouter(
            providers=list(DEFAULT_PROVIDERS),
            default_cooldown_seconds=settings.default_cooldown_seconds,
        )
        self.results = ResultStore(settings.data_dir / "results.json")
        self.quotes = QuoteStore()
        self.verifier = MockVerifier(
            network=settings.x402_network,
            asset=settings.x402_usdc,
            pay_to=settings.x402_pay_to or default_pay_to(),
            price_atomic=settings.x402_price_atomic,
            quotes=self.quotes,
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
        self.job_limiter = JobLimiter(max_concurrent=2)
        # Deliberately do not run a job at construction (that blocked
        # startup and spent quota on every restart).


def _is_loopback(host: str) -> bool:
    return (host or "").strip().lower() in LOOPBACK


def _authorized(header: str, master: str) -> bool:
    if not header.startswith("Bearer "):
        return False
    got = header[7:].encode("utf-8")
    expected = master.encode("utf-8")
    if len(got) != len(expected):
        # compare_digest requires equal length; still do a dummy compare
        # so the timing is not a raw short-circuit on length alone.
        hmac.compare_digest(expected, expected)
        return False
    return hmac.compare_digest(got, expected)


def create_app(
    settings: Settings | None = None,
    environ: Mapping[str, str] | None = None,
) -> FastAPI:
    env = dict(environ if environ is not None else os.environ)
    cfg = settings or load_settings(env)

    if not cfg.master_key:
        if not _is_loopback(cfg.host):
            raise ValueError(
                "refusing to start: LITELLM_MASTER_KEY is unset and the bind "
                f"address is {cfg.host!r}, which is not loopback. Set a master "
                "key, or bind to 127.0.0.1."
            )
        log.warning(
            "MUSE SECURITY WARNING: starting the phase 0 service on %s:%s "
            "WITHOUT a LITELLM_MASTER_KEY. Every endpoint is unauthenticated. "
            "This is permitted only on loopback. Set a master key before "
            "exposing the port, and never bind 0.0.0.0 without one.",
            cfg.host,
            cfg.port,
        )

    app = FastAPI(
        title="Muse Stack phase 0",
        version="0.2.0",
        description=(
            "Routing health, x402 mock settlement on Base Sepolia, and a Lean "
            "useful-work runner. Mock verifier only; no real funds; no mainnet. "
            "Mock Lean results are never sold. Job APIs never return a proof."
        ),
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    # Set eagerly so FastAPI TestClient works without a lifespan context.
    app.state.muse = AppState(cfg, env)

    # Register /docs behind the same auth as the API, by mounting them
    # after the auth middleware rather than leaving FastAPI's defaults
    # on an allow-list.
    def _mount_docs(application: FastAPI) -> None:
        from fastapi.openapi.utils import get_openapi
        from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html

        @application.get("/openapi.json", include_in_schema=False)
        def openapi_spec() -> dict[str, Any]:
            return get_openapi(
                title=application.title,
                version=application.version,
                routes=application.routes,
                description=application.description,
            )

        @application.get("/docs", include_in_schema=False)
        def swagger_ui() -> Any:
            return get_swagger_ui_html(openapi_url="/openapi.json", title=application.title)

        @application.get("/redoc", include_in_schema=False)
        def redoc() -> Any:
            return get_redoc_html(openapi_url="/openapi.json", title=application.title)

    _mount_docs(app)

    @app.middleware("http")
    async def _auth_and_redact(request: Request, call_next):  # type: ignore[no-untyped-def]
        master = cfg.master_key
        if master:
            header = request.headers.get("authorization", "")
            if not _authorized(header, master):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
        try:
            response = await call_next(request)
        except InvalidPaymentHeader:
            return JSONResponse({"error": "malformed payment header"}, status_code=400)
        except InvalidRetryAfter:
            return JSONResponse({"error": "malformed Retry-After header"}, status_code=400)
        except Exception:  # noqa: BLE001
            # Do not leak exception text (paths, types) to the client.
            log.exception("unhandled error")
            return JSONResponse({"error": "internal_error"}, status_code=500)
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
        if event.retry_after is not None and event.retry_after.strip():
            try:
                parse_retry_after(
                    event.retry_after,
                    now_wall=time.time(),
                    default_seconds=cfg.default_cooldown_seconds,
                )
            except InvalidRetryAfter:
                return JSONResponse(
                    {"error": "malformed Retry-After header"},
                    status_code=400,
                )
        state.router.report_event(
            event.provider,
            status_code=event.status_code,
            outcome=event.outcome,
            retry_after=event.retry_after,
            tokens_in=event.tokens_in,
            tokens_out=event.tokens_out,
            error=event.error,
            quota_exhausted=event.quota_exhausted,
            upstream=event.upstream,
        )
        return {"ok": True, "provider": event.provider}

    @app.get("/v1/work/problems")
    def work_problems(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        return {"problems": state.runner.list_problems()}

    @app.post("/v1/work/jobs")
    async def work_jobs(request: Request, body: JobIn) -> JSONResponse:
        import asyncio

        state: AppState = request.app.state.muse
        if not state.job_limiter.acquire():
            return JSONResponse(
                {"error": "too many concurrent jobs"},
                status_code=429,
            )
        try:
            try:
                job = await asyncio.wait_for(
                    asyncio.to_thread(state.runner.run, body.problem_id, models=body.models),
                    timeout=120,
                )
            except KeyError as exc:
                raise HTTPException(status_code=404, detail="unknown problem") from exc
            except TimeoutError as exc:
                return JSONResponse({"error": "job timed out"}, status_code=504)
        finally:
            state.job_limiter.release()

        payload = job.as_dict(include_proof=False)
        # Only a *real* Lean verification is listed for sale. Mock
        # results are never sold.
        if job.verified and job.result_hash and job.proof and job.mode != "mock":
            record = state.results.add(
                result_hash=job.result_hash,
                problem_id=job.problem_id,
                title=job.title or job.problem_id,
                verdict=job.verdict or "verified",
                verifier=job.verifier or "lean4",
                model=job.model or "",
                proof=job.proof,
                statement=job.statement or "",
                price_atomic=cfg.x402_price_atomic,
                metadata=job.record or {},
                result_id=job.job_id,
            )
            payload["listedForSale"] = record.result_id
            return JSONResponse(payload, status_code=201)
        payload["listedForSale"] = None
        return JSONResponse(payload, status_code=200)

    @app.get("/v1/work/jobs/{job_id}")
    def work_job(request: Request, job_id: str) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        job = state.runner.get(job_id)
        if not job:
            raise HTTPException(status_code=404, detail="unknown job")
        # Never return the paid proof on this path.
        return job.as_dict(include_proof=False)

    def _resource_url(request: Request, result_id: str) -> str:
        return str(request.base_url).rstrip("/") + f"/v1/results/{result_id}"

    @app.get("/v1/results")
    def list_results(request: Request) -> dict[str, Any]:
        state: AppState = request.app.state.muse
        return {
            "results": [item.public_view(include_proof=False) for item in state.results.list()],
            "note": "GET a result without a payment to receive HTTP 402; retry with PAYMENT-SIGNATURE bound to the quote.",
        }

    @app.get("/v1/results/{result_id}")
    def get_result(request: Request, result_id: str) -> JSONResponse:
        state: AppState = request.app.state.muse
        record = state.results.get(result_id)
        if record is None:
            raise HTTPException(status_code=404, detail="unknown result")
        try:
            payload = read_payment_header(request.headers)
        except InvalidPaymentHeader:
            return JSONResponse({"error": "malformed payment header"}, status_code=400)
        option = usdc_sepolia_option(
            amount_atomic=record.price_atomic,
            pay_to=cfg.x402_pay_to,
            asset=cfg.x402_usdc,
            max_timeout_seconds=cfg.x402_max_timeout_seconds,
        )
        if payload is None:
            quote = state.quotes.issue(
                result_id=result_id,
                amount=str(record.price_atomic),
                pay_to=cfg.x402_pay_to,
                asset=cfg.x402_usdc,
                network=cfg.x402_network,
                ttl_seconds=cfg.x402_max_timeout_seconds,
                now_wall=time.time(),
            )
            body, headers = payment_required(
                option=option,
                resource_url=_resource_url(request, result_id),
                description=f"Verified Lean result {record.result_hash[:16]}… ({record.problem_id})",
                quote=quote,
            )
            preview = record.public_view(include_proof=False)
            body["result"] = preview
            return JSONResponse(body, status_code=402, headers=headers)
        quote_id = payload.get("quoteId") or (payload.get("payload") or {}).get("quoteId")
        quote = state.quotes.get(str(quote_id)) if quote_id else None
        try:
            verification = state.verifier.verify(
                payload,
                price_atomic=record.price_atomic,
                quote=quote,
                result_id=result_id,
            )
        except PaymentRejected as exc:
            body, headers = payment_required(
                option=option,
                resource_url=_resource_url(request, result_id),
                description=f"Verified Lean result {record.result_hash[:16]}… ({record.problem_id})",
                error=exc.reason,
            )
            body["rejection"] = {"code": exc.code, "reason": exc.reason}
            return JSONResponse(body, status_code=402, headers=headers)
        content = record.public_view(include_proof=True)
        content["receipt"] = verification.as_dict()
        headers = encode_payment_response(verification.as_dict())
        assert_response_safe(content, env)
        # Record the sale only after the response is fully built and
        # declared safe — then mark it as a background task so a failed
        # delivery path (assert, etc.) cannot increment sold_count first.
        receipt_id = verification.receipt_id

        def _mark() -> None:
            try:
                state.results.mark_sold(result_id, receipt_id)
            except Exception:  # noqa: BLE001
                log.exception("failed to record sale after delivery")

        return JSONResponse(
            content,
            status_code=200,
            headers=headers,
            background=BackgroundTask(_mark),
        )

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
    # Re-check bind vs master key against the *effective* host (CLI can
    # override settings). 0.0.0.0 without a key is refused.
    if not settings.master_key and not _is_loopback(host):
        raise SystemExit(
            f"refusing to start: LITELLM_MASTER_KEY is unset and --host {host} "
            "is not loopback. Set a master key or bind to 127.0.0.1."
        )
    # Rebuild settings with the effective host so create_app sees it.
    from dataclasses import replace

    settings = replace(settings, host=host, port=port)
    uvicorn.run(
        lambda: create_app(settings=settings),
        factory=True,
        host=host,
        port=port,
        log_level="info",
    )


if __name__ == "__main__":
    main()
