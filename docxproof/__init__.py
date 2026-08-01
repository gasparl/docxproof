"""Formatting-preserving DOCX proofreading."""

from .cli import easy_proofread
from .engine import make_windows, run_proofreader
from .providers import DeepSeekAdapter, OpenAIAdapter

__all__ = [
    "DeepSeekAdapter",
    "OpenAIAdapter",
    "easy_proofread",
    "make_windows",
    "run_proofreader",
]
