"""Step 1 of ideation: transcript cleanup.

Pure code — no LLM. Operates on timed segments from AWS Transcribe.

Produces:
  - clean_segments: list of {start, end, text, original_text} with filler
    removed and punctuation normalized, but PRESERVING exact timestamps.
  - clean_text: concatenated clean text for LLM analysis.
  - segment_index: maps character positions in clean_text back to the segment
    they came from (for timestamp recovery after LLM processing).

CRITICAL: never break the timestamp mapping. Every word in clean_text must
be traceable back to a timed segment. We REMOVE filler words from the text
used for analysis, but the audio at those timestamps is untouched.
"""
import re
from typing import Any

# Filler patterns — removed from analysis text but timestamps preserved.
_FILLER_PATTERNS = [
    r"\bum\b",
    r"\buh\b",
    r"\buh huh\b",
    r"\byou know\b",
    r"\blike\b(?=\s*,)",  # "like," as filler, not "like something"
    r"\bI mean\b(?=\s*,)",
    r"\bso\b(?=\s*,\s*so\b)",  # "so, so" stutters
    r"\bkind of\b",
    r"\bsort of\b",
    r"\bbasically\b",
    r"\bright\?\s*",  # "right?" as filler tag question
]
_FILLER_RE = re.compile("|".join(_FILLER_PATTERNS), re.IGNORECASE)

# Collapse multiple spaces, fix run-on punctuation.
_MULTI_SPACE = re.compile(r"\s{2,}")
_COMMA_COMMA = re.compile(r",\s*,")


def _clean_text(text: str) -> str:
    """Remove filler words and normalize whitespace/punctuation."""
    cleaned = _FILLER_RE.sub("", text)
    cleaned = _COMMA_COMMA.sub(",", cleaned)
    cleaned = _MULTI_SPACE.sub(" ", cleaned).strip()
    # Remove leading comma/space from a segment after filler removal.
    cleaned = re.sub(r"^[,\s]+", "", cleaned)
    return cleaned


def cleanup(
    timed_segments: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str, list[tuple[int, int, int]]]:
    """Clean timed segments for LLM analysis while preserving timestamp mapping.

    Returns:
        clean_segments: [{start, end, text, original_text, seg_index}, ...]
        clean_text: single string for LLM consumption (with segment markers)
        segment_index: [(char_start, char_end, seg_idx), ...] mapping clean_text
                       character ranges back to clean_segments indices.
    """
    clean_segments: list[dict[str, Any]] = []
    clean_text_parts: list[str] = []
    segment_index: list[tuple[int, int, int]] = []
    char_pos = 0

    for i, seg in enumerate(timed_segments):
        original = seg["text"]
        cleaned = _clean_text(original)
        if not cleaned:
            continue  # entire segment was filler — skip for analysis

        clean_segments.append({
            "start": seg["start"],
            "end": seg["end"],
            "text": cleaned,
            "original_text": original,
            "seg_index": i,
        })
        # Track character position mapping.
        text_with_marker = f"[{len(clean_segments)-1}] {cleaned}"
        segment_index.append((char_pos, char_pos + len(text_with_marker), len(clean_segments) - 1))
        clean_text_parts.append(text_with_marker)
        char_pos += len(text_with_marker) + 1  # +1 for newline

    clean_text = "\n".join(clean_text_parts)
    return clean_segments, clean_text, segment_index


def segments_for_range(
    clean_segments: list[dict[str, Any]],
    start_idx: int,
    end_idx: int,
) -> tuple[float, float, str]:
    """Given a range of clean_segment indices, return (audio_start, audio_end,
    original_text) covering those segments. Uses ORIGINAL text (with filler)
    because the audio is untouched."""
    if start_idx < 0:
        start_idx = 0
    if end_idx >= len(clean_segments):
        end_idx = len(clean_segments) - 1
    span = clean_segments[start_idx : end_idx + 1]
    if not span:
        return 0.0, 0.0, ""
    audio_start = span[0]["start"]
    audio_end = span[-1]["end"]
    original_text = " ".join(s["original_text"] for s in span)
    return audio_start, audio_end, original_text
