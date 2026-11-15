"""Mail adapters.

The AgentMail client is draft-only. It does not send mail.
"""

from muse.mail.agentmail import (
    API_BASE,
    DEFAULT_INBOX,
    DEFAULT_KEY_FILE,
    MAX_BODY_CHARS,
    AgentMailAPIError,
    AgentMailClient,
    AgentMailError,
    AgentMailKeyError,
    AgentMailNotAllowed,
    check_allowed,
    for_model,
    load_api_key,
)

__all__ = [
    "API_BASE",
    "DEFAULT_INBOX",
    "DEFAULT_KEY_FILE",
    "MAX_BODY_CHARS",
    "AgentMailAPIError",
    "AgentMailClient",
    "AgentMailError",
    "AgentMailKeyError",
    "AgentMailNotAllowed",
    "check_allowed",
    "for_model",
    "load_api_key",
]
