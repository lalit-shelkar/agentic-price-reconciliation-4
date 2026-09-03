"""Anthropic implementation of `LlmClient`.

Structured output is obtained with a **forced tool call** rather than by asking
for JSON in the prompt and parsing the reply: `tool_choice={"type": "tool", ...}`
makes the model fill in a schema-validated argument object, so the result arrives
as a dict instead of prose that might be wrapped in markdown, prefixed with
"Here's the JSON:", or truncated mid-object. That removes a whole class of parse
failures from a path that reads untrusted counterparty input.

`anthropic` is an optional dependency (`pip install -e ".[llm]"`), imported inside
`__init__` so the core package — and the entire test suite, which uses
`FakeLlmClient` — installs and runs without it.
"""

from __future__ import annotations

from typing import Any

from .client import (
    LlmAuthError,
    LlmExtraction,
    LlmInvalidResponse,
    LlmQuotaExceeded,
    LlmTimeout,
    LlmUnavailable,
)


class AnthropicLlmClient:
    """`LlmClient` backed by the Anthropic Messages API."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None = None,
        temperature: float = 0.0,
        max_tokens: int = 1024,
        timeout_seconds: float = 30.0,
    ) -> None:
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - depends on optional extra
            raise LlmUnavailable(
                "the `anthropic` package is not installed; "
                'install the optional extra with: pip install -e ".[llm]"'
            ) from exc

        self._anthropic = anthropic
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens
        # `api_key=None` lets the SDK fall back to ANTHROPIC_API_KEY, which is
        # where it should come from — a key passed through config risks ending up
        # in a YAML file next to the thresholds.
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout_seconds)

    @property
    def model_id(self) -> str:
        return self._model

    def extract_json(
        self,
        *,
        system: str,
        user: str,
        json_schema: dict[str, Any],
        schema_name: str,
        schema_description: str = "",
        max_tokens: int | None = None,
    ) -> LlmExtraction:
        anthropic = self._anthropic
        try:
            response = self._client.messages.create(
                model=self._model,
                max_tokens=max_tokens or self._max_tokens,
                temperature=self._temperature,
                system=system,
                messages=[{"role": "user", "content": user}],
                tools=[
                    {
                        "name": schema_name,
                        "description": schema_description or f"Return {schema_name}.",
                        "input_schema": json_schema,
                    }
                ],
                tool_choice={"type": "tool", "name": schema_name},
            )
        except anthropic.APITimeoutError as exc:
            raise LlmTimeout(f"anthropic timed out: {exc}") from exc
        except anthropic.APIConnectionError as exc:
            raise LlmUnavailable(f"anthropic unreachable: {exc}") from exc
        except anthropic.RateLimitError as exc:
            # Mapped to a *non-retryable* error on purpose — see LlmQuotaExceeded.
            raise LlmQuotaExceeded(f"anthropic rate limit/quota: {exc}") from exc
        except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
            raise LlmAuthError(f"anthropic rejected the credential: {exc}") from exc
        except anthropic.APIStatusError as exc:
            if exc.status_code >= 500:
                raise LlmUnavailable(f"anthropic {exc.status_code}: {exc}") from exc
            raise LlmInvalidResponse(f"anthropic {exc.status_code}: {exc}") from exc

        for block in response.content:
            if getattr(block, "type", None) == "tool_use":
                data = block.input
                if not isinstance(data, dict):
                    raise LlmInvalidResponse(
                        f"expected a JSON object from {schema_name}, got {type(data)!r}"
                    )
                usage = getattr(response, "usage", None)
                return LlmExtraction(
                    data=data,
                    model_id=getattr(response, "model", self._model),
                    input_tokens=getattr(usage, "input_tokens", None),
                    output_tokens=getattr(usage, "output_tokens", None),
                )

        # Reachable if the model stops early — e.g. `stop_reason == "max_tokens"`
        # before it finished the tool call. Surfaced rather than papered over with
        # a default, because a silently-empty extraction here would look to the
        # caller exactly like "the email contained no price".
        raise LlmInvalidResponse(
            f"no tool_use block in response (stop_reason="
            f"{getattr(response, 'stop_reason', None)!r}); the extraction did not complete"
        )
