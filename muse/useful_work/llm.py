"""LLM clients used by the useful-work runner.

``LiteLLMClient`` talks to the Muse Stack gateway (OpenAI-compatible).
``MockLLM`` returns scripted proofs so tests and CI never need a provider key
and never print one. Failures are raised as ``CallFailed`` so the gateway
fallback chain can walk the next model/provider.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Mapping, Protocol
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from muse.gateway.fallback import CallFailed
from muse.useful_work.problems import Problem


class LLMClient(Protocol):
    def complete(self, *, model: str, prompt: str, problem: Problem) -> str: ...


@dataclass
class MockLLM:
    """Map problem ids (and optional model names) to a canned proof.

    A value of ``None`` or the empty string simulates a model failure.
    The default answers are the known-good tactics the mock Lean verifier
    accepts for the bundled samples.
    """

    answers: dict[str, str] = field(default_factory=dict)
    fail_models: set[str] = field(default_factory=set)
    default_answer: str | None = "Nat.add_comm a b"

    def complete(self, *, model: str, prompt: str, problem: Problem) -> str:  # noqa: ARG002
        if model in self.fail_models:
            raise CallFailed(
                model,
                f"mock LLM refused model {model}",
                status_code=429,
                retry_after="5",
            )
        key = f"{model}:{problem.problem_id}"
        if key in self.answers:
            return self._emit(model, self.answers[key])
        if problem.problem_id in self.answers:
            return self._emit(model, self.answers[problem.problem_id])
        if self.default_answer is None:
            raise CallFailed(model, "mock LLM has no answer", status_code=500)
        return self._emit(model, self.default_answer)

    def _emit(self, model: str, proof: str) -> str:
        if proof is None or proof == "":
            raise CallFailed(model, "mock LLM returned an empty proof", status_code=500)
        return proof


@dataclass
class LiteLLMClient:
    """Minimal OpenAI-compatible chat client. Does not log the bearer token."""

    base_url: str
    master_key: str = ""
    timeout_seconds: float = 120.0
    max_tokens: int = 1024

    def complete(self, *, model: str, prompt: str, problem: Problem) -> str:  # noqa: ARG002
        url = self.base_url.rstrip("/") + "/v1/chat/completions"
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": "You write Lean 4 proofs and nothing else."},
                {"role": "user", "content": prompt},
            ],
            "temperature": 0,
            "max_tokens": self.max_tokens,
        }
        body = json.dumps(payload).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.master_key:
            headers["Authorization"] = f"Bearer {self.master_key}"
        request = Request(url, data=body, headers=headers, method="POST")
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:  # noqa: S310 - base_url is configured
                status = response.status
                raw = response.read()
                retry_after = response.headers.get("Retry-After")
        except HTTPError as exc:
            retry_after = exc.headers.get("Retry-After") if exc.headers else None
            raise CallFailed(
                model,
                f"LLM HTTP {exc.code}",
                status_code=exc.code,
                retry_after=retry_after,
                cause=exc,
            ) from exc
        except URLError as exc:
            raise CallFailed(model, f"LLM transport error: {exc.reason}", status_code=599, cause=exc) from exc
        if not 200 <= status < 300:
            raise CallFailed(model, f"LLM HTTP {status}", status_code=status, retry_after=retry_after)
        data: Mapping[str, Any] = json.loads(raw.decode("utf-8"))
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise CallFailed(model, "LLM response missing choices[0].message.content", status_code=status) from exc
        if not isinstance(content, str) or not content.strip():
            raise CallFailed(model, "LLM returned an empty completion", status_code=status)
        return _strip_fences(content)


def _strip_fences(text: str) -> str:
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        body = "\n".join(lines).strip()
    return body
