"""Routing layer: circuit breaker, fallback chains, health, and upstream maps.

LiteLLM 1.108.5 already honours Retry-After on its own deployments. This
package is the Muse-side breaker (useful-work runner, quota-exhausted
state, cross-alias cooldown) and the health/usage endpoints.
"""

from muse.gateway.circuit_breaker import CircuitBreaker, CircuitOpen, CircuitState
from muse.gateway.fallback import FallbackChain, FallbackExhausted
from muse.gateway.health import HealthStore
from muse.gateway.router import GatewayRouter

__all__ = [
    "CircuitBreaker",
    "CircuitOpen",
    "CircuitState",
    "FallbackChain",
    "FallbackExhausted",
    "GatewayRouter",
    "HealthStore",
]
