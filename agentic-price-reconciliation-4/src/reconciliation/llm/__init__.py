"""Provider-agnostic LLM access.

See `client.py`'s module docstring for where an LLM is and isn't allowed to run in
this system — the short version is: only behind a `tools/contracts.py` tool
protocol, never in an agent's decision logic.
"""

from .client import (
    LlmAuthError,
    LlmClient,
    LlmError,
    LlmExtraction,
    LlmInvalidResponse,
    LlmQuotaExceeded,
    LlmTimeout,
    LlmUnavailable,
)
from .factory import build_llm_client
from .fake import FakeLlmClient

__all__ = [
    "FakeLlmClient",
    "LlmAuthError",
    "LlmClient",
    "LlmError",
    "LlmExtraction",
    "LlmInvalidResponse",
    "LlmQuotaExceeded",
    "LlmTimeout",
    "LlmUnavailable",
    "build_llm_client",
]
