"""Synchronous AI provider adapters."""

from __future__ import annotations

import json
import re
from typing import Any, Protocol

from pydantic import ValidationError

from .schemas import CorrectionBatch
from .settings import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_MAX_OUTPUT_TOKENS,
    DEFAULT_REASONING_EFFORT,
    OPENAI_MAX_OUTPUT_TOKENS,
    get_api_key,
)


class RetryableModelError(RuntimeError):
    """A malformed or incomplete model response that is safe to retry."""


class ModelAdapter(Protocol):
    provider: str
    model: str
    retryable_exceptions: tuple[type[BaseException], ...]

    def complete(self, system_prompt: str, user_prompt: str) -> CorrectionBatch:
        ...


def _strip_json_fences(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    return stripped.strip()


class OpenAIAdapter:
    provider = "openai"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = OPENAI_MAX_OUTPUT_TOKENS,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    ) -> None:
        try:
            from openai import (
                APIConnectionError,
                APITimeoutError,
                OpenAI,
                OpenAIError,
                RateLimitError,
            )
        except ImportError as exc:
            raise RuntimeError("Install dependencies with: python -m pip install -e .") from exc

        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.client = OpenAI(api_key=api_key, timeout=180.0)
        self.retryable_exceptions = (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            RetryableModelError,
        )
        self.openai_error = OpenAIError

    def complete(self, system_prompt: str, user_prompt: str) -> CorrectionBatch:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "text_format": CorrectionBatch,
            "max_output_tokens": self.max_output_tokens,
            "store": False,
        }
        if self.reasoning_effort:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}

        response = self.client.responses.parse(**kwargs)
        if getattr(response, "status", None) == "incomplete":
            details = getattr(response, "incomplete_details", None)
            raise RetryableModelError(f"OpenAI response incomplete: {details}")

        parsed = response.output_parsed
        if parsed is None:
            refusal = getattr(response, "refusal", None)
            if refusal:
                raise RuntimeError(f"Model refusal: {refusal}")
            raise RetryableModelError("OpenAI returned no parsed structured output")
        return parsed


class DeepSeekAdapter:
    provider = "deepseek"

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        max_output_tokens: int = DEEPSEEK_MAX_OUTPUT_TOKENS,
        reasoning_effort: str = "high",
    ) -> None:
        try:
            from openai import (
                APIConnectionError,
                APITimeoutError,
                OpenAI,
                OpenAIError,
                RateLimitError,
            )
        except ImportError as exc:
            raise RuntimeError("Install dependencies with: python -m pip install -e .") from exc

        self.model = model
        self.max_output_tokens = max_output_tokens
        self.reasoning_effort = reasoning_effort
        self.client = OpenAI(
            api_key=api_key,
            base_url=DEEPSEEK_BASE_URL,
            timeout=240.0,
        )
        self.retryable_exceptions = (
            RateLimitError,
            APIConnectionError,
            APITimeoutError,
            RetryableModelError,
        )
        self.openai_error = OpenAIError

    def complete(self, system_prompt: str, user_prompt: str) -> CorrectionBatch:
        schema = json.dumps(CorrectionBatch.model_json_schema(), ensure_ascii=False)
        deepseek_system_prompt = (
            f"{system_prompt}\n\nReturn one JSON object conforming exactly to this JSON schema:\n{schema}"
        )
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": deepseek_system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_output_tokens,
            "reasoning_effort": self.reasoning_effort,
<<<<<<< HEAD
            "extra_body": {"thinking": {"type": "enabled"}},
        }
=======
        }
        if self.reasoning_effort != "low":
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
>>>>>>> 9b73426 (corrections)

        response = self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        if choice.finish_reason in {"length", "insufficient_system_resource"}:
            raise RetryableModelError(
                f"DeepSeek response ended with finish_reason={choice.finish_reason}"
            )
        content = choice.message.content or ""
        if not content.strip():
            raise RetryableModelError("DeepSeek returned empty JSON output")
        try:
            payload = json.loads(_strip_json_fences(content))
            return CorrectionBatch.model_validate(payload)
        except (json.JSONDecodeError, ValidationError) as exc:
            raise RetryableModelError(f"Invalid DeepSeek JSON output: {exc}") from exc


<<<<<<< HEAD
=======
def _resolve_deepseek_reasoning_effort(model: str, reasoning_effort: str) -> str:
    if reasoning_effort == "none":
        return "low"
    if model.endswith("-flash") and reasoning_effort == "medium":
        return "low"
    if reasoning_effort == "medium":
        return "high"
    return reasoning_effort


>>>>>>> 9b73426 (corrections)
def build_adapter(
    provider: str,
    model: str,
    config: dict[str, Any],
    *,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
) -> ModelAdapter:
    api_key = get_api_key(provider, config)
    if provider == "openai":
        return OpenAIAdapter(
            api_key=api_key,
            model=model,
            max_output_tokens=max_output_tokens or OPENAI_MAX_OUTPUT_TOKENS,
            reasoning_effort=reasoning_effort,
        )
    if provider == "deepseek":
<<<<<<< HEAD
        deepseek_effort = "high" if reasoning_effort == "medium" else reasoning_effort
        if deepseek_effort == "none":
            deepseek_effort = "low"
=======
        deepseek_effort = _resolve_deepseek_reasoning_effort(model, reasoning_effort)
>>>>>>> 9b73426 (corrections)
        return DeepSeekAdapter(
            api_key=api_key,
            model=model,
            max_output_tokens=max_output_tokens or DEEPSEEK_MAX_OUTPUT_TOKENS,
            reasoning_effort=deepseek_effort,
        )
    raise ValueError(f"Unsupported provider: {provider}")
