#!/usr/bin/env python3
"""Simple launcher. Edit the settings below or pass input/output paths."""

from __future__ import annotations

# ======================== USER SETTINGS ========================
AI_PROVIDER = "openai"       # "openai" or "deepseek"
MODEL = "gpt-5.6-terra"      # OpenAI: gpt-5.6-terra | gpt-5.6
                              # DeepSeek: deepseek-v4-pro | deepseek-v4-flash
INPUT_DOCX = "input.docx"
OUTPUT_DOCX = None            # None -> output/<input name>_proofread.docx beside input

WINDOW_WORDS = 400
OVERLAP_WORDS = 100
VERIFY_SUGGESTIONS = True
REASONING_EFFORT = "medium"  # low | medium | high
# ===============================================================

import argparse
from pathlib import Path

from docxproof import easy_proofread


def _resolve_from_working_directory(value: str | Path) -> Path:
    """Resolve relative paths from the directory where the command was run."""
    return Path(value).expanduser().resolve()


def _default_config_path(launcher_dir: Path) -> Path | None:
    """Prefer config.json in the working directory, then beside this launcher."""
    cwd_config = Path.cwd() / "config.json"
    if cwd_config.exists():
        return cwd_config
    launcher_config = launcher_dir / "config.json"
    return launcher_config if launcher_config.exists() else None
def main() -> None:
    parser = argparse.ArgumentParser(description="Proofread a DOCX.")
    parser.add_argument("input", nargs="?", default=INPUT_DOCX)
    parser.add_argument("output", nargs="?", default=OUTPUT_DOCX)
    args = parser.parse_args()

    launcher_dir = Path(__file__).resolve().parent
    input_path = _resolve_from_working_directory(args.input)
    output_path = (
        _resolve_from_working_directory(args.output)
        if args.output is not None
        else None
    )

    easy_proofread(
        input_docx=input_path,
        output_docx=output_path,
        provider=AI_PROVIDER,
        model=MODEL,
        window_words=WINDOW_WORDS,
        overlap_words=OVERLAP_WORDS,
        verify=VERIFY_SUGGESTIONS,
        reasoning_effort=REASONING_EFFORT,
        config_path=_default_config_path(launcher_dir),
    )


if __name__ == "__main__":
    main()
