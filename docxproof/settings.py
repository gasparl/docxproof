"""Defaults and the small set of supported provider/model choices."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

AVAILABLE_PROVIDERS = ("openai", "deepseek")

MODEL_OPTIONS: dict[str, tuple[str, ...]] = {
    "openai": ("gpt-5.6-terra", "gpt-5.6"),
    "deepseek": ("deepseek-v4-pro", "deepseek-v4-flash"),
}
DEFAULT_PROVIDER = "openai"
DEFAULT_MODELS = {
    "openai": "gpt-5.6-terra",
    "deepseek": "deepseek-v4-pro",
}

# Standard OpenAI-compatible DeepSeek endpoint. It is intentionally not routine
# user configuration; change it here only when using a proxy or custom gateway.
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

WINDOW_WORDS = 400
OVERLAP_WORDS = 100
OPENAI_MAX_OUTPUT_TOKENS = 2500
DEEPSEEK_MAX_OUTPUT_TOKENS = 8192
RETRIES = 5
WRITE_EVERY = 1
VERIFY_SUGGESTIONS = True
DEFAULT_REASONING_EFFORT = "medium"
DEFAULT_PARTS = ("main", "headers", "footers", "footnotes", "endnotes")

# Bump whenever prompts, window semantics, or correction reconciliation changes.
PROMPT_VERSION = "docx-proofreader-v5-context-precedence"
EDITABLE_START = "<<<PROOFREADER_EDITABLE_START_8C1B>>>"
EDITABLE_END = "<<<PROOFREADER_EDITABLE_END_8C1B>>>"


def resolve_model(provider: str, model: str | None) -> str:
    provider = provider.lower()
    if provider not in AVAILABLE_PROVIDERS:
        raise ValueError(f"provider must be one of: {', '.join(AVAILABLE_PROVIDERS)}")
    resolved = model or DEFAULT_MODELS[provider]
    if resolved not in MODEL_OPTIONS[provider]:
        allowed = ", ".join(MODEL_OPTIONS[provider])
        raise ValueError(f"Unsupported {provider} model {resolved!r}. Choose: {allowed}")
    return resolved


def find_config_path(path: str | Path | None = None) -> Path | None:
    if path is not None:
        return Path(path).expanduser().resolve()
    candidate = Path.cwd() / "config.json"
    return candidate if candidate.exists() else None


def load_config(path: str | Path | None = None) -> dict[str, Any]:
    config_path = find_config_path(path)
    if config_path is None or not config_path.exists():
        return {}
    try:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Invalid config file {config_path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise RuntimeError(f"Config file {config_path} must contain a JSON object")
    return payload


def get_api_key(provider: str, config: dict[str, Any]) -> str:
    env_name = "OPENAI_API_KEY" if provider == "openai" else "DEEPSEEK_API_KEY"
    value = os.environ.get(env_name) or config.get(env_name)
    if not value:
        raise RuntimeError(
            f"Missing {env_name}. Set it as an environment variable or in config.json."
        )
    return str(value)
