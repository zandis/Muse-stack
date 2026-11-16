"""Wardmuse resource manager: one person's own AI keys, quotas and local models.

The LiteLLM proxy does the routing. This package adds what it lacks: a usage
ledger, quota and reset tracking, privacy classes, a key store, local-model
discovery, a CLI and a local dashboard.
"""

__version__ = "0.1.0"
