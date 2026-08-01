"""Public helper and command-line interface."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Sequence

from .engine import run_proofreader
from .providers import build_adapter
from .settings import (
    AVAILABLE_PROVIDERS,
    DEFAULT_PARTS,
    DEFAULT_REASONING_EFFORT,
    MODEL_OPTIONS,
    OVERLAP_WORDS,
    RETRIES,
    VERIFY_SUGGESTIONS,
    WINDOW_WORDS,
    load_config,
    resolve_provider_and_model,
)


def default_output_path(input_path: Path) -> Path:
    return input_path.parent / "output" / f"{input_path.stem}_proofread{input_path.suffix}"


def easy_proofread(
    input_docx: str | Path = "input.docx",
    output_docx: str | Path | None = None,
    *,
    provider: str | None = None,
    model: str | None = None,
    reasoning_effort: str = DEFAULT_REASONING_EFFORT,
    max_output_tokens: int | None = None,
    window_words: int = WINDOW_WORDS,
    overlap_words: int = OVERLAP_WORDS,
    verify: bool = VERIFY_SUGGESTIONS,
    include: Sequence[str] = DEFAULT_PARTS,
    config_path: str | Path | None = None,
):
    """Create a proofread DOCX while preserving the original package structure."""
    provider, model = resolve_provider_and_model(provider, model)
    input_path = Path(input_docx).expanduser().resolve()
    output_path = (
        Path(output_docx).expanduser().resolve()
        if output_docx is not None
        else default_output_path(input_path)
    )

    adapter = build_adapter(
        provider,
        model,
        load_config(config_path),
        reasoning_effort=reasoning_effort,
        max_output_tokens=max_output_tokens,
    )

    print(f"Proofreading {input_path.name} with {provider}/{model} ...")
    result = run_proofreader(
        input_path,
        output_path,
        adapter,
        include=include,
        window_words=window_words,
        overlap_words=overlap_words,
        verify=verify,
        report_txt_path=output_path.with_suffix(".proofreading.txt"),
        report_json_path=output_path.with_suffix(".proofreading.json"),
    )
    print(f"Output: {output_path}")
    print(f"Applied corrections: {len(result.accepted)}")
    if result.failed_windows:
        print(f"Warning: {result.failed_windows} window(s) failed.")
    return result


def _parse_include(value: str) -> tuple[str, ...]:
    valid = {"main", "headers", "footers", "footnotes", "endnotes", "comments"}
    parts = tuple(item.strip().lower() for item in value.split(",") if item.strip())
    unknown = set(parts) - valid
    if unknown:
        raise argparse.ArgumentTypeError(f"Unknown part(s): {', '.join(sorted(unknown))}")
    return parts


def build_arg_parser() -> argparse.ArgumentParser:
    model_help = "; ".join(
        f"{provider}: {', '.join(models)}" for provider, models in MODEL_OPTIONS.items()
    )
    parser = argparse.ArgumentParser(
        prog="docx-proofread",
        description="Proofread a DOCX and create a formatting-preserving corrected copy.",
    )
    parser.add_argument("input", help="Input DOCX")
    parser.add_argument(
        "output",
        nargs="?",
        help="Output DOCX (default: output/<name>_proofread.docx beside input)",
    )
    parser.add_argument(
        "--provider",
        choices=AVAILABLE_PROVIDERS,
        help="Provider (optional when --model uniquely identifies it)",
    )
    parser.add_argument("--model", help=f"Supported models: {model_help}")
    parser.add_argument("--config", help="Path to config.json (default: ./config.json)")
    parser.add_argument("--window-words", type=int, default=WINDOW_WORDS)
    parser.add_argument("--overlap-words", type=int, default=OVERLAP_WORDS)
    parser.add_argument(
        "--include",
        type=_parse_include,
        default=DEFAULT_PARTS,
        help="Comma-separated document parts",
    )
    parser.add_argument(
        "--verify",
        action=argparse.BooleanOptionalAction,
        default=VERIFY_SUGGESTIONS,
        help="Use a second pass to remove questionable corrections",
    )
    parser.add_argument(
        "--reasoning",
        choices=("none", "low", "medium", "high"),
        default=DEFAULT_REASONING_EFFORT,
    )
    parser.add_argument(
        "--max-output-tokens",
        type=int,
        help="Override provider output token limit for a single run",
    )
    parser.add_argument("--retries", type=int, default=RETRIES)
    parser.add_argument("--checkpoint")
    parser.add_argument("--keep-checkpoint", action="store_true")
    parser.add_argument("--fail-on-window-error", action="store_true")
    return parser


def cli_main(argv: Sequence[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    try:
        provider, model = resolve_provider_and_model(args.provider, args.model)
        input_path = Path(args.input).expanduser().resolve()
        output_path = (
            Path(args.output).expanduser().resolve()
            if args.output
            else default_output_path(input_path)
        )
        adapter = build_adapter(
            provider,
            model,
            load_config(args.config),
            reasoning_effort=args.reasoning,
            max_output_tokens=args.max_output_tokens,
        )
        result = run_proofreader(
            input_path,
            output_path,
            adapter,
            include=args.include,
            window_words=args.window_words,
            overlap_words=args.overlap_words,
            verify=args.verify,
            retries=args.retries,
            checkpoint_path=Path(args.checkpoint).resolve() if args.checkpoint else None,
            report_txt_path=output_path.with_suffix(".proofreading.txt"),
            report_json_path=output_path.with_suffix(".proofreading.json"),
            keep_checkpoint=args.keep_checkpoint,
            fail_on_window_error=args.fail_on_window_error,
        )
    except KeyboardInterrupt:
        print("Interrupted. The saved checkpoint can be reused on the next run.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    print(f"Output: {output_path}")
    print(f"Applied corrections: {len(result.accepted)}")
    if result.failed_windows:
        print(f"Warning: {result.failed_windows} window(s) failed.")
    return 0
