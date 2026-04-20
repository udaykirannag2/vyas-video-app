"""Align text passages to timed transcript segments.

Two modes:
  1. align_by_phrases(start_phrase, end_phrase, segments) — find the segments
     that contain the start phrase and end phrase respectively, extract
     everything between them. Preferred mode (from two-pass ideation).
  2. align_passage(passage_text, segments) — fuzzy sliding-window match.
     Fallback mode when start/end phrases aren't available.
"""
import re
from typing import Any


def _normalize(text: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation."""
    t = text.lower()
    t = re.sub(r"[^a-z0-9\s]", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _find_segment_containing(
    phrase: str,
    segments: list[dict[str, Any]],
    *,
    search_from: int = 0,
) -> int:
    """Return the index of the segment whose text best matches the phrase.
    Uses normalized substring matching: checks which segment's neighborhood
    (segment + previous + next) contains the most words of the phrase."""
    phrase_norm = _normalize(phrase)
    phrase_words = phrase_norm.split()
    if not phrase_words:
        return search_from

    best_idx = search_from
    best_hits = 0

    for i in range(search_from, len(segments)):
        # Build a context window: prev + current + next segment
        context_parts = []
        if i > 0:
            context_parts.append(segments[i - 1]["text"])
        context_parts.append(segments[i]["text"])
        if i < len(segments) - 1:
            context_parts.append(segments[i + 1]["text"])
        context_norm = _normalize(" ".join(context_parts))

        # Count how many phrase words appear in context
        hits = sum(1 for w in phrase_words if w in context_norm.split())
        if hits > best_hits:
            best_hits = hits
            best_idx = i

    return best_idx


def align_by_phrases(
    start_phrase: str,
    end_phrase: str,
    segments: list[dict[str, Any]],
    *,
    max_duration: float = 185.0,  # 3-min reel cap + 5s buffer
) -> tuple[float, float, str]:
    """Find segments between start_phrase and end_phrase.

    Returns (window_start, window_end, verbatim_text).
    Raises ValueError if alignment fails.
    """
    if not segments:
        raise ValueError("empty segments")
    if not start_phrase.strip() or not end_phrase.strip():
        raise ValueError("start_phrase and end_phrase required")

    start_idx = _find_segment_containing(start_phrase, segments, search_from=0)
    end_idx = _find_segment_containing(end_phrase, segments, search_from=start_idx)

    # Ensure end is after start
    if end_idx <= start_idx:
        end_idx = min(start_idx + 8, len(segments) - 1)

    # Cap at max_duration
    while (
        end_idx > start_idx
        and segments[end_idx]["end"] - segments[start_idx]["start"] > max_duration
    ):
        end_idx -= 1

    matched = segments[start_idx : end_idx + 1]
    window_start = matched[0]["start"]
    window_end = matched[-1]["end"]
    text = " ".join(seg["text"] for seg in matched)

    return window_start, window_end, text


# ---------- Fallback: full-passage alignment ----------


def _overlap_ratio(a: str, b: str) -> float:
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return 0.0
    return len(wa & wb) / len(wa | wb)


def align_passage(
    passage_text: str,
    timed_segments: list[dict[str, Any]],
    *,
    max_window_segments: int = 30,
) -> tuple[float, float, str]:
    """Fallback: sliding-window fuzzy match of full passage text."""
    if not timed_segments or not passage_text.strip():
        raise ValueError("empty input to align_passage")

    passage_norm = _normalize(passage_text)
    n = len(timed_segments)
    best_score = -1.0
    best_span = (0, 0)

    for length in range(3, min(max_window_segments, n) + 1):
        for start_idx in range(n - length + 1):
            end_idx = start_idx + length
            span_text = " ".join(
                seg["text"] for seg in timed_segments[start_idx:end_idx]
            )
            span_norm = _normalize(span_text)
            if len(span_norm) < len(passage_norm) * 0.4:
                continue
            if len(span_norm) > len(passage_norm) * 2.5:
                continue
            score = _overlap_ratio(passage_norm, span_norm)
            if score > best_score:
                best_score = score
                best_span = (start_idx, end_idx)

    if best_score < 0.3:
        raise ValueError(
            f"No good alignment found (best score {best_score:.2f}). "
            f"Passage starts with: {passage_text[:80]!r}"
        )

    start_idx, end_idx = best_span
    matched_segs = timed_segments[start_idx:end_idx]
    return (
        matched_segs[0]["start"],
        matched_segs[-1]["end"],
        " ".join(seg["text"] for seg in matched_segs),
    )
