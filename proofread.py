#!/usr/bin/env python3
"""Simple launcher. Edit the settings below, then run this file."""

from __future__ import annotations

# ======================== USER SETTINGS ========================
AI_PROVIDER = "openai"       # "openai" or "deepseek"
MODEL = "gpt-5.6-terra"      # OpenAI: gpt-5.6-terra | gpt-5.6
                              # DeepSeek: deepseek-v4-pro | deepseek-v4-flash
INPUT_DOCX = "input.docx"
OUTPUT_DOCX = None            # None -> <input name>_proofread.docx

WINDOW_WORDS = 400
OVERLAP_WORDS = 100
VERIFY_SUGGESTIONS = True
REASONING_EFFORT = "medium"  # low | medium | high
# ===============================================================

import argparse
from pathlib import Path

from docxproof import easy_proofread


def main() -> None:
    parser = argparse.ArgumentParser(description="Proofread a DOCX.")
    parser.add_argument("input", nargs="?", default=INPUT_DOCX)
    parser.add_argument("output", nargs="?", default=OUTPUT_DOCX)
    args = parser.parse_args()

    base_dir = Path(__file__).resolve().parent
    input_path = Path(args.input).expanduser()
    if not input_path.is_absolute():
        input_path = base_dir / input_path

    output_path = Path(args.output).expanduser() if args.output else None
    if output_path is not None and not output_path.is_absolute():
        output_path = base_dir / output_path

    easy_proofread(
        input_docx=input_path,
        output_docx=output_path,
        provider=AI_PROVIDER,
        model=MODEL,
        window_words=WINDOW_WORDS,
        overlap_words=OVERLAP_WORDS,
        verify=VERIFY_SUGGESTIONS,
        reasoning_effort=REASONING_EFFORT,
        config_path=base_dir / "config.json",
    )


if __name__ == "__main__":
    main()
