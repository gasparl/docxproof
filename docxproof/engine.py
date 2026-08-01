"""DOCX extraction, overlapping windows, validation, patching, and pipeline."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import random
import re
import shutil
import tempfile
import time
import zipfile
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Sequence

from lxml import etree
from pydantic import ValidationError

from .providers import ModelAdapter
from .schemas import (
    Correction,
    CorrectionBatch,
    ParagraphRecord,
    ProposedPatch,
    RejectedPatch,
    RunResult,
    StoryPart,
    SYSTEM_PROMPT,
    TextAtom,
    VERIFY_PROMPT,
    Window,
    WordRecord,
)
from .settings import (
    DEFAULT_PARTS,
    EDITABLE_END,
    EDITABLE_START,
    OVERLAP_WORDS,
    PROMPT_VERSION,
    RETRIES,
    VERIFY_SUGGESTIONS,
    WINDOW_WORDS,
    WRITE_EVERY,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    datefmt="%H:%M:%S",
)
for noisy_logger in ("openai", "httpx", "httpcore"):
    logging.getLogger(noisy_logger).setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
XML_NS = "http://www.w3.org/XML/1998/namespace"
NS = {"w": W_NS}

W_P = f"{{{W_NS}}}p"
W_R = f"{{{W_NS}}}r"
W_T = f"{{{W_NS}}}t"
W_TAB = f"{{{W_NS}}}tab"
W_BR = f"{{{W_NS}}}br"
W_CR = f"{{{W_NS}}}cr"
W_DEL = f"{{{W_NS}}}del"
W_FLD_SIMPLE = f"{{{W_NS}}}fldSimple"
W_FLD_CHAR = f"{{{W_NS}}}fldChar"
W_VANISH = f"{{{W_NS}}}vanish"
W_FLD_CHAR_TYPE = f"{{{W_NS}}}fldCharType"
XML_SPACE = f"{{{XML_NS}}}space"

WORD_RE = re.compile(r"[^\W_]+(?:[’'\-][^\W_]+)*", re.UNICODE)

def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(text)
        os.replace(temp_name, path)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def all_occurrences(text: str, needle: str) -> list[int]:
    starts: list[int] = []
    pos = 0
    while True:
        found = text.find(needle, pos)
        if found < 0:
            return starts
        starts.append(found)
        pos = found + 1



def part_slug(part_name: str) -> str:
    if part_name == "word/document.xml":
        return "D"
    match = re.fullmatch(r"word/header(\d+)\.xml", part_name)
    if match:
        return f"H{match.group(1)}"
    match = re.fullmatch(r"word/footer(\d+)\.xml", part_name)
    if match:
        return f"F{match.group(1)}"
    if part_name == "word/footnotes.xml":
        return "FN"
    if part_name == "word/endnotes.xml":
        return "EN"
    if part_name == "word/comments.xml":
        return "C"
    return re.sub(r"[^A-Za-z0-9]+", "_", Path(part_name).stem).upper()[:12]


def selected_part_names(names: Iterable[str], include: Sequence[str]) -> list[str]:
    include_set = set(include)
    selected: list[str] = []
    for name in names:
        if name == "word/document.xml" and "main" in include_set:
            selected.append(name)
        elif re.fullmatch(r"word/header\d+\.xml", name) and "headers" in include_set:
            selected.append(name)
        elif re.fullmatch(r"word/footer\d+\.xml", name) and "footers" in include_set:
            selected.append(name)
        elif name == "word/footnotes.xml" and "footnotes" in include_set:
            selected.append(name)
        elif name == "word/endnotes.xml" and "endnotes" in include_set:
            selected.append(name)
        elif name == "word/comments.xml" and "comments" in include_set:
            selected.append(name)
    return selected


# ---------------------------------------------------------------------------
# OOXML extraction
# ---------------------------------------------------------------------------

def _has_ancestor(element: etree._Element, tag: str) -> bool:
    return any(ancestor.tag == tag for ancestor in element.iterancestors())


def _run_is_hidden(element: etree._Element) -> bool:
    run = next((a for a in element.iterancestors() if a.tag == W_R), None)
    if run is None:
        return False
    rpr = run.find(f"{{{W_NS}}}rPr")
    return rpr is not None and rpr.find(W_VANISH) is not None


def extract_paragraph_text(paragraph: etree._Element) -> tuple[str, list[TextAtom]]:
    pieces: list[str] = []
    atoms: list[TextAtom] = []
    cursor = 0
    field_depth = 0

    for node in paragraph.iter():
        if node.tag == W_FLD_CHAR:
            field_type = node.get(W_FLD_CHAR_TYPE)
            if field_type == "begin":
                field_depth += 1
            elif field_type == "end" and field_depth > 0:
                field_depth -= 1
            continue

        if node.tag not in (W_T, W_TAB, W_BR, W_CR):
            continue
        if field_depth > 0:
            continue
        if _has_ancestor(node, W_DEL) or _has_ancestor(node, W_FLD_SIMPLE):
            continue
        if _run_is_hidden(node):
            continue

        if node.tag == W_T:
            value = node.text or ""
            kind = "text"
            element: etree._Element | None = node
        elif node.tag == W_TAB:
            value = "\t"
            kind = "tab"
            element = None
        else:
            value = "\n"
            kind = "break"
            element = None

        start = cursor
        cursor += len(value)
        pieces.append(value)
        atoms.append(TextAtom(start=start, end=cursor, kind=kind, element=element))

    return "".join(pieces), atoms


def parse_story(part_name: str, xml_bytes: bytes) -> StoryPart:
    parser = etree.XMLParser(
        resolve_entities=False,
        remove_blank_text=False,
        recover=False,
        huge_tree=True,
    )
    root = etree.fromstring(xml_bytes, parser=parser)
    slug = part_slug(part_name)
    paragraphs: list[ParagraphRecord] = []

    for index, paragraph in enumerate(root.iter(W_P), start=1):
        text, atoms = extract_paragraph_text(paragraph)
        paragraphs.append(
            ParagraphRecord(
                part_name=part_name,
                paragraph_id=f"{slug}-P{index:06d}",
                element=paragraph,
                text=text,
                atoms=atoms,
            )
        )

    story_pieces: list[str] = []
    cursor = 0
    for index, paragraph in enumerate(paragraphs):
        if index:
            story_pieces.append("\n\n")
            cursor += 2
        paragraph.story_start = cursor
        story_pieces.append(paragraph.text)
        cursor += len(paragraph.text)
        paragraph.story_end = cursor

    story_text = "".join(story_pieces)
    words = [WordRecord(m.start(), m.end()) for m in WORD_RE.finditer(story_text)]
    return StoryPart(
        part_name=part_name,
        xml_bytes=xml_bytes,
        root=root,
        paragraphs=paragraphs,
        text=story_text,
        words=words,
    )


def load_docx_stories(input_docx: Path, include: Sequence[str]) -> tuple[dict[str, bytes], list[StoryPart]]:
    if not zipfile.is_zipfile(input_docx):
        raise ValueError(f"Not a valid DOCX/ZIP package: {input_docx}")

    with zipfile.ZipFile(input_docx, "r") as zin:
        package = {info.filename: zin.read(info.filename) for info in zin.infolist()}
        names = list(package)

    parts = selected_part_names(names, include)
    if "word/document.xml" not in parts and "main" in include:
        raise ValueError("The DOCX package does not contain word/document.xml")

    stories: list[StoryPart] = []
    for name in parts:
        try:
            stories.append(parse_story(name, package[name]))
        except etree.XMLSyntaxError as exc:
            raise ValueError(f"Could not parse {name}: {exc}") from exc
    return package, stories


# ---------------------------------------------------------------------------
# Window construction with overlapping review and precedence metadata
# ---------------------------------------------------------------------------

def _boundary_score(story: StoryPart, word_index: int) -> tuple[int, int]:
    """Return (quality, punctuation_strength) for a boundary before word_index."""
    if word_index <= 0 or word_index >= len(story.words):
        return (4, 0)
    left = story.words[word_index - 1]
    right = story.words[word_index]
    gap = story.text[left.end : right.start]
    left_context = story.text[max(0, left.end - 4) : right.start]

    if "\n\n" in gap:
        return (4, 0)
    if "\n" in gap:
        return (3, 0)
    if re.search(r"[.!?][\"'”’)]*\s*$", left_context):
        return (2, 2)
    if re.search(r"[;:][\"'”’)]*\s*$", left_context):
        return (1, 1)
    return (0, 0)


def choose_ownership_boundary(story: StoryPart, low: int, high: int) -> int:
    """Choose a word boundary inside an overlap, favoring structural boundaries."""
    if low > high:
        raise ValueError("Invalid overlap boundary range")
    midpoint = (low + high) / 2.0
    candidates = range(low, high + 1)
    return min(
        candidates,
        key=lambda k: (
            -_boundary_score(story, k)[0],
            abs(k - midpoint),
            k,
        ),
    )


def _word_boundary_char(story: StoryPart, word_index: int) -> int:
    """Return the character boundary before word_index.

    Inter-word punctuation is normally owned by the word on its left, so a
    sentence-final period remains editable. At a paragraph boundary, or when
    opening punctuation appears after whitespace, the boundary is moved before
    that opening punctuation so it belongs to the following word/window.
    """
    if word_index <= 0:
        return 0
    if word_index >= len(story.words):
        return len(story.text)

    left = story.words[word_index - 1]
    right = story.words[word_index]
    gap = story.text[left.end : right.start]

    paragraph_break = gap.rfind("\n\n")
    if paragraph_break >= 0:
        return left.end + paragraph_break + 2

    # Example: '. "Next' -> put the boundary after the whitespace and before
    # the opening quote. If the gap ends in whitespace, right.start is correct.
    match = re.search(r"\s+([^\s]+)$", gap)
    if match:
        return left.end + match.start(1)
    return right.start


def render_window_text(
    story: StoryPart,
    context_word_start: int,
    context_word_end: int,
) -> str:
    """Render one fully reviewable context window with stable paragraph labels."""
    context_start = _word_boundary_char(story, context_word_start)
    context_end = _word_boundary_char(story, context_word_end)

    events: list[tuple[int, int, str]] = []
    # At equal positions: editable-end, paragraph label, editable-start. This
    # keeps labels outside the marked review region.
    for paragraph in story.paragraphs:
        if paragraph.story_end < context_start or paragraph.story_start > context_end:
            continue
        label_pos = max(context_start, paragraph.story_start)
        # If the paragraph actually began before this window, the visible text
        # is a mid-paragraph (often mid-sentence) continuation. Flag that
        # explicitly so the model does not mistake the excerpt's first visible
        # word for the paragraph's true, capitalization-worthy start.
        continued = paragraph.story_start < context_start
        suffix = " continued" if continued else ""
        events.append((label_pos, 1, f"[[{paragraph.paragraph_id}{suffix}]] "))

    events.append((context_end, 0, EDITABLE_END))
    events.append((context_start, 2, EDITABLE_START))
    events.sort(key=lambda event: (event[0], event[1]))

    output: list[str] = []
    cursor = context_start
    for position, _priority, inserted in events:
        position = min(max(position, context_start), context_end)
        if position < cursor:
            continue
        output.append(story.text[cursor:position])
        output.append(inserted)
        cursor = position
    output.append(story.text[cursor:context_end])
    return "".join(output)


def make_windows(story: StoryPart, window_words: int, overlap_words: int) -> list[Window]:
    if window_words <= 0:
        raise ValueError("window_words must be positive")
    if overlap_words < 0 or overlap_words >= window_words:
        raise ValueError("overlap_words must satisfy 0 <= overlap_words < window_words")
    if not story.words:
        return []

    step = window_words - overlap_words
    contexts: list[tuple[int, int]] = []
    start = 0
    total_words = len(story.words)
    while start < total_words:
        end = min(start + window_words, total_words)
        contexts.append((start, end))
        if end >= total_words:
            break
        start += step

    ownership_boundaries: list[int] = [0]
    for current, following in zip(contexts, contexts[1:]):
        overlap_low = following[0]
        overlap_high = current[1]
        ownership_boundaries.append(
            choose_ownership_boundary(story, overlap_low, overlap_high)
        )
    ownership_boundaries.append(total_words)

    windows: list[Window] = []
    for index, ((context_start, context_end), target_start, target_end) in enumerate(
        zip(contexts, ownership_boundaries, ownership_boundaries[1:]),
        start=1,
    ):
        rendered = render_window_text(
            story,
            context_start,
            context_end,
        )
        windows.append(
            Window(
                part_name=story.part_name,
                index=index,
                context_word_start=context_start,
                context_word_end=context_end,
                target_word_start=target_start,
                target_word_end=target_end,
                rendered_text=rendered,
            )
        )
    return windows


# ---------------------------------------------------------------------------

# Sequential request control and checkpointing
# ---------------------------------------------------------------------------

def call_with_retries(
    adapter: ModelAdapter,
    system_prompt: str,
    user_prompt: str,
    retries: int,
) -> CorrectionBatch:
    """Call one provider request at a time with exponential retry backoff."""
    def _compact_error(exc: BaseException) -> str:
        text = str(exc).strip().replace("\n", " ")
        if len(text) > 120:
            return text[:117] + "..."
        return text

    last_error: BaseException | None = None
    for attempt in range(retries):
        try:
            return adapter.complete(system_prompt, user_prompt)
        except adapter.retryable_exceptions as exc:  # type: ignore[misc]
            last_error = exc
        except (RuntimeError, ValidationError, json.JSONDecodeError) as exc:
            # Malformed or empty structured output is often transient.
            last_error = exc
        except Exception:
            raise

        if attempt == retries - 1:
            break
        wait = min(60.0, 2**attempt + random.random())
        logger.warning(
            "Retry %d/%d in %.1fs: %s",
            attempt + 1,
            retries,
            wait,
            _compact_error(last_error),
        )
        time.sleep(wait)

    assert last_error is not None
    raise last_error


def checkpoint_metadata(
    input_hash: str,
    provider: str,
    model: str,
    window_words: int,
    overlap_words: int,
    verify: bool,
    include: Sequence[str],
) -> dict[str, Any]:
    return {
        "prompt_version": PROMPT_VERSION,
        "input_sha256": input_hash,
        "provider": provider,
        "model": model,
        "window_words": window_words,
        "overlap_words": overlap_words,
        "verify": verify,
        "include": list(include),
    }


def load_checkpoint(path: Path, expected_meta: dict[str, Any]) -> dict[str, Any]:
    if not path.exists():
        return {"metadata": expected_meta, "windows": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("Ignoring invalid checkpoint %s: %s", path, exc)
        return {"metadata": expected_meta, "windows": {}}
    if data.get("metadata") != expected_meta:
        logger.info("Checkpoint settings/input changed; starting a fresh checkpoint.")
        return {"metadata": expected_meta, "windows": {}}
    if not isinstance(data.get("windows"), dict):
        return {"metadata": expected_meta, "windows": {}}
    return data


def save_checkpoint(path: Path, checkpoint: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(checkpoint, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Model pass, verification, and validation
# ---------------------------------------------------------------------------

def correction_identity(correction: Correction) -> tuple[str, str, str, int]:
    return (
        correction.paragraph_id,
        correction.original,
        correction.replacement,
        correction.occurrence,
    )


def verification_user_prompt(window: Window, proposed: CorrectionBatch) -> str:
    return (
        f"{VERIFY_PROMPT}\n\n"
        "EXCERPT:\n"
        f"{window.rendered_text}\n\n"
        "PROPOSED CORRECTIONS (JSON):\n"
        f"{proposed.model_dump_json(indent=2)}"
    )


def proofread_window(
    adapter: ModelAdapter,
    window: Window,
    verify: bool,
    retries: int,
    debug_last_window: Path | None,
) -> CorrectionBatch:
    if debug_last_window:
        debug_last_window.write_text(
            f"=== {window.key} ===\n{window.rendered_text}", encoding="utf-8"
        )

    first = call_with_retries(
        adapter,
        SYSTEM_PROMPT,
        window.rendered_text,
        retries,
    )
    if not verify or not first.corrections:
        return first

    verified = call_with_retries(
        adapter,
        SYSTEM_PROMPT,
        verification_user_prompt(window, first),
        retries,
    )

    proposed_by_id = {correction_identity(c): c for c in first.corrections}
    kept: list[Correction] = []
    seen: set[tuple[str, str, str, int]] = set()
    for candidate in verified.corrections:
        identity = correction_identity(candidate)
        if identity in proposed_by_id and identity not in seen:
            kept.append(proposed_by_id[identity])
            seen.add(identity)
    return CorrectionBatch(corrections=kept)


def touched_word_indices(story: StoryPart, global_start: int, global_end: int) -> list[int]:
    indices: list[int] = []
    for index, word in enumerate(story.words):
        if global_start == global_end:
            if word.start <= global_start <= word.end:
                indices.append(index)
            elif global_start < word.start:
                break
        elif word.end > global_start and word.start < global_end:
            indices.append(index)
        elif word.start >= global_end:
            break
    return indices


def range_is_editable(paragraph: ParagraphRecord, start: int, end: int) -> bool:
    if start < 0 or end < start or end > len(paragraph.text):
        return False
    if start == end:
        # An insertion is allowed at a boundary touching at least one text atom.
        return any(
            atom.editable and atom.start <= start <= atom.end
            for atom in paragraph.atoms
        )

    covered = [False] * (end - start)
    for atom in paragraph.atoms:
        overlap_start = max(start, atom.start)
        overlap_end = min(end, atom.end)
        if overlap_start >= overlap_end:
            continue
        if not atom.editable:
            return False
        for pos in range(overlap_start, overlap_end):
            covered[pos - start] = True
    return all(covered)


def _range_word_span(
    story: StoryPart,
    global_start: int,
    global_end: int,
) -> tuple[int, int]:
    """Return the half-open word span associated with a character edit."""
    touched = touched_word_indices(story, global_start, global_end)
    if touched:
        return min(touched), max(touched) + 1

    # Punctuation-only edit or insertion: associate it with the nearest
    # following word, or the previous word at the end of the story.
    nearest = next(
        (i for i, word in enumerate(story.words) if word.start >= global_start),
        len(story.words) - 1,
    )
    return nearest, nearest + 1


def _range_within_word_bounds(
    story: StoryPart,
    global_start: int,
    global_end: int,
    word_start: int,
    word_end: int,
) -> bool:
    range_start, range_end = _range_word_span(story, global_start, global_end)
    return range_start >= word_start and range_end <= word_end


def validate_batch(
    story: StoryPart,
    window: Window,
    batch: CorrectionBatch,
    max_edit_chars: int = 240,
) -> tuple[list[ProposedPatch], list[RejectedPatch]]:
    """Validate model patches against the complete visible context window.

    Adjacent windows may validate the same source range. Reconciliation happens
    later, after equivalent edits have been reduced to their minimal difference.
    """
    paragraph_map = story.paragraph_map
    accepted: list[ProposedPatch] = []
    rejected: list[RejectedPatch] = []

    def reject(correction: Correction, why: str) -> None:
        rejected.append(
            RejectedPatch(
                part_name=story.part_name,
                window_key=window.key,
                paragraph_id=correction.paragraph_id,
                original=correction.original,
                replacement=correction.replacement,
                reason=correction.reason,
                rejection=why,
            )
        )

    context_char_start = _word_boundary_char(story, window.context_word_start)
    context_char_end = _word_boundary_char(story, window.context_word_end)

    for correction in batch.corrections:
        paragraph = paragraph_map.get(correction.paragraph_id)
        if paragraph is None:
            # Models occasionally echo the full excerpt label, including the
            # "continued" marker used for mid-paragraph windows.
            trimmed_id = correction.paragraph_id.split(" ", 1)[0]
            paragraph = paragraph_map.get(trimmed_id)
        if paragraph is None:
            reject(correction, "unknown paragraph_id")
            continue
        if correction.original == correction.replacement:
            reject(correction, "replacement is identical to original")
            continue
        if any(
            marker in correction.original or marker in correction.replacement
            for marker in (EDITABLE_START, EDITABLE_END, "[[")
        ):
            reject(correction, "correction contains a label or editable marker")
            continue
        if "\n" in correction.original or "\r" in correction.original:
            reject(correction, "correction crosses a line or paragraph boundary")
            continue
        if "\n" in correction.replacement or "\r" in correction.replacement:
            reject(correction, "replacement would alter paragraph structure")
            continue
        if max(len(correction.original), len(correction.replacement)) > max_edit_chars:
            reject(correction, f"correction exceeds {max_edit_chars} characters")
            continue

        candidate_occurrences: list[int] = []
        for occurrence_start in all_occurrences(paragraph.text, correction.original):
            occurrence_end = occurrence_start + len(correction.original)
            occurrence_global_start = paragraph.story_start + occurrence_start
            occurrence_global_end = paragraph.story_start + occurrence_end
            if (
                occurrence_global_start >= context_char_start
                and occurrence_global_end <= context_char_end
            ):
                candidate_occurrences.append(occurrence_start)

        if correction.occurrence > len(candidate_occurrences):
            reject(
                correction,
                f"occurrence {correction.occurrence} not found exactly inside this window",
            )
            continue

        full_local_start = candidate_occurrences[correction.occurrence - 1]
        full_local_end = full_local_start + len(correction.original)
        if not range_is_editable(paragraph, full_local_start, full_local_end):
            reject(
                correction,
                "range touches a field, tab, break, hidden text, or other protected content",
            )
            continue

        prefix, suffix = common_affixes(correction.original, correction.replacement)
        original_end = len(correction.original) - suffix if suffix else len(correction.original)
        replacement_end = (
            len(correction.replacement) - suffix
            if suffix
            else len(correction.replacement)
        )
        minimal_original = correction.original[prefix:original_end]
        minimal_replacement = correction.replacement[prefix:replacement_end]
        local_start = full_local_start + prefix
        local_end = full_local_end - suffix
        global_start = paragraph.story_start + local_start
        global_end = paragraph.story_start + local_end

        if minimal_original == minimal_replacement:
            reject(correction, "correction has no effective difference after normalization")
            continue
        if not range_is_editable(paragraph, local_start, local_end):
            reject(correction, "minimal range is not safely editable")
            continue
        correction_word_start, _correction_word_end = _range_word_span(
            story,
            global_start,
            global_end,
        )
        if (
            paragraph.story_start < context_char_start
            and correction_word_start == window.context_word_start
            and minimal_original.lower() == minimal_replacement.lower()
        ):
            reject(
                correction,
                "case-only edit targets the first visible word of a continuing paragraph",
            )
            continue
        if not _range_within_word_bounds(
            story,
            global_start,
            global_end,
            window.context_word_start,
            window.context_word_end,
        ):
            reject(correction, "correction is outside this window's visible context")
            continue

        primary_owner_seen = _range_within_word_bounds(
            story,
            global_start,
            global_end,
            window.target_word_start,
            window.target_word_end,
        )
        correction_word_start, correction_word_end = _range_word_span(
            story,
            global_start,
            global_end,
        )

        accepted.append(
            ProposedPatch(
                part_name=story.part_name,
                window_key=window.key,
                paragraph_id=correction.paragraph_id,
                original=minimal_original,
                replacement=minimal_replacement,
                occurrence=correction.occurrence,
                reason=correction.reason,
                local_start=local_start,
                local_end=local_end,
                global_start=global_start,
                global_end=global_end,
                source_windows=(window.key,),
                primary_owner_seen=primary_owner_seen,
                window_index=window.index,
                preceding_context_words=max(
                    0, correction_word_start - window.context_word_start
                ),
                following_context_words=max(
                    0, window.context_word_end - correction_word_end
                ),
            )
        )

    return accepted, rejected


def _patch_ranges_overlap(left: ProposedPatch, right: ProposedPatch) -> bool:
    if left.local_start == left.local_end and right.local_start == right.local_end:
        return left.local_start == right.local_start
    if left.local_start == left.local_end:
        return right.local_start <= left.local_start <= right.local_end
    if right.local_start == right.local_end:
        return left.local_start <= right.local_start <= left.local_end
    return left.local_start < right.local_end and right.local_start < left.local_end


def _reject_from_patch(patch: ProposedPatch, why: str) -> RejectedPatch:
    return RejectedPatch(
        part_name=patch.part_name,
        window_key=patch.window_key,
        paragraph_id=patch.paragraph_id,
        original=patch.original,
        replacement=patch.replacement,
        reason=patch.reason,
        rejection=why,
    )


def _patch_priority(patch: ProposedPatch) -> tuple[int, int, int, int]:
    """Rank competing edits deterministically.

    More preceding context is the main criterion. Primary ownership and
    following context are secondary safeguards; an earlier window wins the
    final tie. Identical edits are merged before this ranking is used.
    """
    return (
        patch.preceding_context_words,
        int(patch.primary_owner_seen),
        patch.following_context_words,
        -patch.window_index,
    )


def _overlap_components(patches: Sequence[ProposedPatch]) -> list[list[ProposedPatch]]:
    """Return connected components under range overlap."""
    remaining = set(range(len(patches)))
    components: list[list[ProposedPatch]] = []
    while remaining:
        seed = remaining.pop()
        component = {seed}
        frontier = [seed]
        while frontier:
            current = frontier.pop()
            connected = {
                candidate
                for candidate in remaining
                if _patch_ranges_overlap(patches[current], patches[candidate])
            }
            remaining.difference_update(connected)
            component.update(connected)
            frontier.extend(connected)
        components.append([patches[i] for i in sorted(component)])
    return components


def resolve_conflicts(
    patches: Sequence[ProposedPatch],
) -> tuple[list[ProposedPatch], list[RejectedPatch]]:
    """Merge corroboration and resolve overlap disagreements by context.

    Identical normalized edits are merged. When different edits overlap, the
    proposal made with more preceding context takes precedence. Lower-ranked
    alternatives are rejected without cancelling the selected edit. A later
    window can still add a non-overlapping correction, or supply a correction
    that no earlier window proposed. Equal-precedence conflicts remain
    unchanged because there is no deterministic basis for choosing one.
    """
    by_paragraph: dict[tuple[str, str], list[ProposedPatch]] = {}
    for patch in patches:
        by_paragraph.setdefault((patch.part_name, patch.paragraph_id), []).append(patch)

    accepted: list[ProposedPatch] = []
    rejected: list[RejectedPatch] = []

    for _key, paragraph_group in by_paragraph.items():
        # First merge exact agreement across windows. This preserves
        # corroboration without allowing duplicate edits to compete.
        identical_groups: dict[tuple[int, int, str], list[ProposedPatch]] = {}
        for patch in paragraph_group:
            identical_groups.setdefault(
                (patch.local_start, patch.local_end, patch.replacement), []
            ).append(patch)

        consolidated: list[ProposedPatch] = []
        for same_edit in identical_groups.values():
            representative = max(same_edit, key=_patch_priority)
            windows = tuple(
                dict.fromkeys(
                    window
                    for patch in sorted(same_edit, key=_patch_priority, reverse=True)
                    for window in patch.source_windows
                )
            )
            consolidated.append(
                replace(
                    representative,
                    source_windows=windows,
                    primary_owner_seen=any(p.primary_owner_seen for p in same_edit),
                )
            )

        # Each connected overlap cluster is independent. Within a cluster,
        # process precedence tiers from strongest to weakest.
        for component in _overlap_components(consolidated):
            if len(component) == 1:
                accepted.append(component[0])
                continue

            chosen: list[ProposedPatch] = []
            blocked: list[ProposedPatch] = []
            priorities = sorted({_patch_priority(p) for p in component}, reverse=True)

            for priority in priorities:
                tier = [p for p in component if _patch_priority(p) == priority]
                eligible: list[ProposedPatch] = []

                for patch in tier:
                    if any(_patch_ranges_overlap(patch, winner) for winner in chosen):
                        rejected.append(
                            _reject_from_patch(
                                patch,
                                "superseded by a higher-precedence correction "
                                "proposed with more preceding context",
                            )
                        )
                    elif any(_patch_ranges_overlap(patch, item) for item in blocked):
                        rejected.append(
                            _reject_from_patch(
                                patch,
                                "overlaps an unresolved higher-precedence conflict; "
                                "left unchanged",
                            )
                        )
                    else:
                        eligible.append(patch)

                if not eligible:
                    continue

                for same_priority_component in _overlap_components(eligible):
                    if len(same_priority_component) == 1:
                        winner = same_priority_component[0]
                        chosen.append(winner)
                        accepted.append(winner)
                    else:
                        why = (
                            "equal-precedence corrections overlap or disagree; "
                            "no deterministic winner was available"
                        )
                        blocked.extend(same_priority_component)
                        rejected.extend(
                            _reject_from_patch(patch, why)
                            for patch in same_priority_component
                        )

    return accepted, rejected


# ---------------------------------------------------------------------------
# Applying validated patches while preserving run formatting
# ---------------------------------------------------------------------------

def set_text_element(element: etree._Element, value: str) -> None:
    element.text = value
    if value.startswith(" ") or value.endswith(" "):
        element.set(XML_SPACE, "preserve")
    else:
        element.attrib.pop(XML_SPACE, None)


def common_affixes(original: str, replacement: str) -> tuple[int, int]:
    prefix = 0
    max_prefix = min(len(original), len(replacement))
    while prefix < max_prefix and original[prefix] == replacement[prefix]:
        prefix += 1

    suffix = 0
    max_suffix = min(len(original) - prefix, len(replacement) - prefix)
    while suffix < max_suffix and original[-1 - suffix] == replacement[-1 - suffix]:
        suffix += 1
    return prefix, suffix


def replace_paragraph_range(
    paragraph_element: etree._Element,
    start: int,
    end: int,
    replacement: str,
) -> None:
    current_text, atoms = extract_paragraph_text(paragraph_element)
    if start < 0 or end < start or end > len(current_text):
        raise ValueError("Patch range is outside the current paragraph")

    editable_atoms = [atom for atom in atoms if atom.editable]
    if start == end:
        candidate: TextAtom | None = None
        # Prefer the preceding text run at a run boundary; otherwise use following.
        for atom in editable_atoms:
            if atom.start < start <= atom.end:
                candidate = atom
                break
        if candidate is None:
            for atom in editable_atoms:
                if atom.start <= start < atom.end or atom.start == start:
                    candidate = atom
                    break
        if candidate is None or candidate.element is None:
            raise ValueError("No editable text node at insertion point")
        offset = min(max(start - candidate.start, 0), candidate.end - candidate.start)
        value = candidate.element.text or ""
        set_text_element(candidate.element, value[:offset] + replacement + value[offset:])
        return

    overlapping = [
        atom
        for atom in atoms
        if atom.end > start and atom.start < end
    ]
    if not overlapping or any(not atom.editable for atom in overlapping):
        raise ValueError("Patch touches protected/non-text content")

    first = overlapping[0]
    last = overlapping[-1]
    assert first.element is not None and last.element is not None

    first_value = first.element.text or ""
    last_value = last.element.text or ""
    prefix = first_value[: start - first.start]
    suffix = last_value[end - last.start :]

    if first is last:
        set_text_element(first.element, prefix + replacement + suffix)
        return

    set_text_element(first.element, prefix + replacement)
    for atom in overlapping[1:-1]:
        assert atom.element is not None
        set_text_element(atom.element, "")
    set_text_element(last.element, suffix)


def apply_patch(paragraph: ParagraphRecord, patch: ProposedPatch) -> None:
    current_text, _atoms = extract_paragraph_text(paragraph.element)
    if current_text[patch.local_start : patch.local_end] != patch.original:
        raise ValueError(
            f"Paragraph changed unexpectedly before applying {patch.paragraph_id}: "
            f"expected {patch.original!r}"
        )

    prefix_len, suffix_len = common_affixes(patch.original, patch.replacement)
    old_middle_end = len(patch.original) - suffix_len if suffix_len else len(patch.original)
    new_middle_end = len(patch.replacement) - suffix_len if suffix_len else len(patch.replacement)

    replace_start = patch.local_start + prefix_len
    replace_end = patch.local_start + old_middle_end
    replacement_middle = patch.replacement[prefix_len:new_middle_end]
    replace_paragraph_range(paragraph.element, replace_start, replace_end, replacement_middle)


def apply_patches(stories: Sequence[StoryPart], patches: Sequence[ProposedPatch]) -> None:
    story_map = {story.part_name: story for story in stories}
    paragraph_maps = {story.part_name: story.paragraph_map for story in stories}

    groups: dict[tuple[str, str], list[ProposedPatch]] = {}
    for patch in patches:
        groups.setdefault((patch.part_name, patch.paragraph_id), []).append(patch)

    for (part_name, paragraph_id), group in groups.items():
        paragraph = paragraph_maps[part_name][paragraph_id]
        # Descending order keeps earlier original offsets stable.
        for patch in sorted(group, key=lambda p: (p.local_start, p.local_end), reverse=True):
            apply_patch(paragraph, patch)
        story_map[part_name].changed = True


def serialize_story(story: StoryPart) -> bytes:
    return etree.tostring(
        story.root,
        encoding="UTF-8",
        xml_declaration=True,
        standalone=True,
    )


def write_output_docx(
    input_docx: Path,
    output_docx: Path,
    stories: Sequence[StoryPart],
) -> None:
    replacements = {
        story.part_name: serialize_story(story)
        for story in stories
        if story.changed
    }
    output_docx.parent.mkdir(parents=True, exist_ok=True)

    fd, temp_name = tempfile.mkstemp(
        prefix=f".{output_docx.name}.", suffix=".docx", dir=output_docx.parent
    )
    os.close(fd)
    try:
        with zipfile.ZipFile(input_docx, "r") as zin, zipfile.ZipFile(temp_name, "w") as zout:
            for info in zin.infolist():
                data = replacements.get(info.filename, zin.read(info.filename))
                zout.writestr(info, data)
        os.replace(temp_name, output_docx)
    except Exception:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temp_name)
        raise


def verify_output_docx(path: Path) -> None:
    """Perform a structural integrity check on the generated DOCX package."""
    required = {"[Content_Types].xml", "_rels/.rels", "word/document.xml"}
    with zipfile.ZipFile(path, "r") as docx_zip:
        bad_member = docx_zip.testzip()
        if bad_member:
            raise ValueError(f"Generated DOCX has a corrupt ZIP member: {bad_member}")
        missing = required - set(docx_zip.namelist())
        if missing:
            raise ValueError(
                "Generated DOCX is missing required package parts: "
                + ", ".join(sorted(missing))
            )
        parser = etree.XMLParser(
            resolve_entities=False, remove_blank_text=False, recover=False, huge_tree=True
        )
        for member in ("[Content_Types].xml", "_rels/.rels", "word/document.xml"):
            etree.fromstring(docx_zip.read(member), parser=parser)


# ---------------------------------------------------------------------------
# Reports
# ---------------------------------------------------------------------------

def report_text(
    input_docx: Path,
    output_docx: Path,
    provider: str,
    model: str,
    window_words: int,
    overlap_words: int,
    result: RunResult,
) -> str:
    lines = [
        "--- Proofreading Report ---",
        f"Provider: {provider}",
        f"Model: {model}",
        f"Date: {datetime.now().isoformat(timespec='seconds')}",
        f"Source file: {input_docx.name}",
        f"Output file: {output_docx.name}",
        f"Window words: {window_words}",
        f"Overlap words: {overlap_words}",
        f"Completed windows: {result.completed_windows}",
        f"Failed windows: {result.failed_windows}",
        f"Applied corrections: {len(result.accepted)}",
        f"Corroborated by multiple windows: {sum(p.corroborated for p in result.accepted)}",
        f"Found only by a non-primary overlap view: {sum(not p.primary_owner_seen for p in result.accepted)}",
        f"Rejected/unsafe suggestions: {len(result.rejected)}",
        "---",
        "",
    ]
    if not result.accepted:
        lines.append("No validated corrections were applied.")
    else:
        for index, patch in enumerate(result.accepted, start=1):
            lines.extend(
                [
                    f"{index}. [{patch.paragraph_id}] {patch.original!r} -> {patch.replacement!r}",
                    f"   {patch.reason}",
                    f"   Source windows: {len(patch.source_windows)}",
                    f"   Preceding context used for precedence: {patch.preceding_context_words} words",
                ]
            )
    if result.rejected:
        lines.extend(["", "--- Rejected suggestions (not applied) ---"])
        for patch in result.rejected:
            lines.append(
                f"[{patch.paragraph_id}] {patch.original!r} -> {patch.replacement!r}: "
                f"{patch.rejection}"
            )
    return "\n".join(lines) + "\n"


def report_json(
    input_docx: Path,
    output_docx: Path,
    provider: str,
    model: str,
    window_words: int,
    overlap_words: int,
    result: RunResult,
) -> str:
    payload = {
        "provider": provider,
        "model": model,
        "date": datetime.now().isoformat(timespec="seconds"),
        "source_file": str(input_docx),
        "output_file": str(output_docx),
        "window_words": window_words,
        "overlap_words": overlap_words,
        "completed_windows": result.completed_windows,
        "failed_windows": result.failed_windows,
        "corroborated_corrections": sum(p.corroborated for p in result.accepted),
        "overlap_only_corrections": sum(not p.primary_owner_seen for p in result.accepted),
        "accepted": [asdict(item) for item in result.accepted],
        "rejected": [asdict(item) for item in result.rejected],
    }
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# Main proofreading pipeline
# ---------------------------------------------------------------------------

def run_proofreader(
    input_docx: Path,
    output_docx: Path,
    adapter: ModelAdapter,
    *,
    include: Sequence[str] = DEFAULT_PARTS,
    window_words: int = WINDOW_WORDS,
    overlap_words: int = OVERLAP_WORDS,
    verify: bool = VERIFY_SUGGESTIONS,
    retries: int = RETRIES,
    checkpoint_path: Path | None = None,
    write_every: int = WRITE_EVERY,
    report_txt_path: Path | None = None,
    report_json_path: Path | None = None,
    debug_last_window: Path | None = None,
    keep_checkpoint: bool = False,
    fail_on_window_error: bool = False,
) -> RunResult:
    """Proofread windows sequentially, then reconcile overlap edits by context."""
    input_docx = input_docx.resolve()
    output_docx = output_docx.resolve()
    if input_docx == output_docx:
        raise ValueError("Input and output paths must be different")
    if not input_docx.exists():
        raise FileNotFoundError(input_docx)

    package, stories = load_docx_stories(input_docx, include)
    del package  # The writer streams the original ZIP again.

    windows: list[tuple[StoryPart, Window]] = []
    for story in stories:
        story_windows = make_windows(story, window_words, overlap_words)
        logger.info(
            "%s: %d paragraphs, %d words, %d windows",
            story.part_name,
            len(story.paragraphs),
            len(story.words),
            len(story_windows),
        )
        windows.extend((story, window) for window in story_windows)

    if checkpoint_path is None:
        checkpoint_path = output_docx.with_suffix(output_docx.suffix + ".checkpoint.json")
    meta = checkpoint_metadata(
        sha256_file(input_docx),
        adapter.provider,
        adapter.model,
        window_words,
        overlap_words,
        verify,
        include,
    )
    checkpoint = load_checkpoint(checkpoint_path, meta)
    cached_windows: dict[str, Any] = checkpoint["windows"]

    result = RunResult()
    all_validated: list[ProposedPatch] = []
    start_time = time.time()
    total = len(windows)
    unsaved = 0

    for completed, (story, window) in enumerate(windows, start=1):
        error: str | None = None
        cached = cached_windows.get(window.key)
        if cached is not None:
            try:
                batch = CorrectionBatch.model_validate(cached)
                logger.debug("Using checkpoint for %s", window.key)
            except ValidationError:
                logger.warning("Ignoring invalid cached result for %s", window.key)
                cached = None

        if cached is None:
            try:
                logger.debug("Proofreading %s", window.key)
                batch = proofread_window(
                    adapter,
                    window,
                    verify,
                    retries,
                    debug_last_window,
                )
            except Exception as exc:
                batch = CorrectionBatch()
                error = f"{type(exc).__name__}: {exc}"

        if error:
            result.failed_windows += 1
            logger.error("Window %s failed (%s)", window.key, error)
            if fail_on_window_error:
                save_checkpoint(checkpoint_path, checkpoint)
                raise RuntimeError(f"Window {window.key} failed: {error}")
        else:
            result.completed_windows += 1
            cached_windows[window.key] = batch.model_dump(mode="json")
            validated, rejected = validate_batch(story, window, batch)
            all_validated.extend(validated)
            result.rejected.extend(rejected)

        unsaved += 1
        if unsaved >= max(1, write_every) or completed == total:
            save_checkpoint(checkpoint_path, checkpoint)
            unsaved = 0

        elapsed = time.time() - start_time
        average = elapsed / completed
        remaining_seconds = average * (total - completed)
        eta = datetime.now() + timedelta(seconds=remaining_seconds)
        logger.info(
            "Progress %d/%d; ETA %s (ca. %s left)",
            completed,
            total,
            eta.strftime("%H:%M"),
            str(timedelta(seconds=max(0, int(remaining_seconds)))),
        )

    accepted, conflict_rejections = resolve_conflicts(all_validated)
    result.accepted = sorted(
        accepted,
        key=lambda patch: (patch.part_name, patch.paragraph_id, patch.local_start),
    )
    result.rejected.extend(conflict_rejections)

    if result.failed_windows and not fail_on_window_error:
        logger.warning(
            "%d window(s) failed. Text visible only in those windows may remain unchecked.",
            result.failed_windows,
        )

    if result.accepted:
        apply_patches(stories, result.accepted)
        write_output_docx(input_docx, output_docx, stories)
    else:
        output_docx.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(input_docx, output_docx)
    verify_output_docx(output_docx)

    if report_txt_path:
        atomic_write_text(
            report_txt_path,
            report_text(
                input_docx,
                output_docx,
                adapter.provider,
                adapter.model,
                window_words,
                overlap_words,
                result,
            ),
        )
    if report_json_path:
        atomic_write_text(
            report_json_path,
            report_json(
                input_docx,
                output_docx,
                adapter.provider,
                adapter.model,
                window_words,
                overlap_words,
                result,
            ),
        )

    if not keep_checkpoint and result.failed_windows == 0:
        with contextlib.suppress(FileNotFoundError):
            checkpoint_path.unlink()
    elif result.failed_windows:
        logger.info("Checkpoint retained because some windows failed: %s", checkpoint_path)

    logger.info("Proofreading completed. Output written to %s", output_docx)
    return result


# ---------------------------------------------------------------------------
