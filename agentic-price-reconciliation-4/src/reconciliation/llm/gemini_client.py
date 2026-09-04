from __future__ import annotations

import json
from typing import Any

from .client import (
    LlmAuthError,
    LlmExtraction,
    LlmInvalidResponse,
    LlmQuotaExceeded,
    LlmTimeout,
    LlmUnavailable,
)


class GeminiLlmClient:
    """LlmClient backed by Google Gemini."""

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
            from google import genai
        except ImportError as exc:
            raise LlmUnavailable(
                "the `google-genai` package is not installed; "
                'install with: pip install google-genai'
            ) from exc

        if not api_key:
            raise LlmAuthError("Gemini API key is required.")

        self._genai = genai
        self._model = model
        self._temperature = temperature
        self._max_tokens = max_tokens

        self._client = genai.Client(api_key=api_key)

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
        from google.genai import types

        try:
            response = self._client.models.generate_content(
                model=self._model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    temperature=self._temperature,
                    max_output_tokens=max_tokens or self._max_tokens,
                    response_mime_type="application/json",
                    response_schema=json_schema,
                ),
            )

        except TimeoutError as exc:
            raise LlmTimeout(f"gemini timed out: {exc}") from exc

        except Exception as exc:
            error_text = str(exc).lower()

            if "api key" in error_text or "authentication" in error_text:
                raise LlmAuthError(f"gemini auth error: {exc}") from exc

            if "quota" in error_text or "rate limit" in error_text:
                raise LlmQuotaExceeded(f"gemini quota/rate limit: {exc}") from exc

            if "503" in error_text or "500" in error_text:
                raise LlmUnavailable(f"gemini unavailable: {exc}") from exc

            raise LlmUnavailable(f"gemini error: {exc}") from exc

        try:
            data = json.loads(response.text)
        except Exception as exc:
            raise LlmInvalidResponse(
                f"gemini returned invalid json: {exc}"
            ) from exc

        if not isinstance(data, dict):
            raise LlmInvalidResponse(
                f"expected JSON object, got {type(data)!r}"
            )

        usage = getattr(response, "usage_metadata", None)

        return LlmExtraction(
            data=data,
            model_id=self._model,
            input_tokens=getattr(
                usage,
                "prompt_token_count",
                None,
            ),
            output_tokens=getattr(
                usage,
                "candidates_token_count",
                None,
            ),
        )