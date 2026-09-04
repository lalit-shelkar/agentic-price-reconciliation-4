"""Provider selection — the one place that knows which providers exist.

Swapping models is a config change, not a code change:

```yaml
llm:
  provider: anthropic       # or: fake
  model: claude-sonnet-5
```

Adding a provider means adding an `LlmClient` implementation and one entry in
`_PROVIDERS`. Nothing in `tools/adapters/`, `agent1/` or `agent2/` refers to a
provider by name.
"""

from __future__ import annotations

import os
from typing import Callable

from ..config.settings import LlmSettings
from .client import LlmClient, LlmAuthError
from .fake import FakeLlmClient


def _build_gemini(settings: LlmSettings) -> LlmClient:
    from .gemini_client import GeminiLlmClient

    api_key = os.environ.get(settings.api_key_env_var)

    if not api_key:
        raise LlmAuthError(
            f"provider 'gemini' needs an API key in "
            f"${settings.api_key_env_var}"
        )

    return GeminiLlmClient(
        model=settings.model,
        api_key=api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout_seconds=settings.timeout_seconds,
    )

def _build_anthropic(settings: LlmSettings) -> LlmClient:
    from .anthropic_client import AnthropicLlmClient

    api_key = os.environ.get(settings.api_key_env_var)
    if not api_key:
        raise LlmAuthError(
            f"provider 'anthropic' needs an API key in ${settings.api_key_env_var}. "
            "Set it, or use provider 'fake' to run without one."
        )
    return AnthropicLlmClient(
        model=settings.model,
        api_key=api_key,
        temperature=settings.temperature,
        max_tokens=settings.max_tokens,
        timeout_seconds=settings.timeout_seconds,
    )


def _build_fake(settings: LlmSettings) -> LlmClient:
    return FakeLlmClient(model=f"fake/{settings.model}")


_PROVIDERS: dict[str, Callable[[LlmSettings], LlmClient]] = {
    "anthropic": _build_anthropic,
    "gemini": _build_gemini,
    "fake": _build_fake,
}


def build_llm_client(settings: LlmSettings) -> LlmClient:
    try:
        factory = _PROVIDERS[settings.provider]
    except KeyError:
        known = ", ".join(sorted(_PROVIDERS))
        raise ValueError(
            f"unknown llm provider {settings.provider!r}; known providers: {known}"
        ) from None
    return factory(settings)
