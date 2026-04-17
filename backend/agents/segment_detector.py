"""Step 2 of ideation: semantic segment detection.

LLM reads the cleaned transcript (with [N] segment markers) and identifies
TOPIC BOUNDARIES — where distinct self-contained ideas begin and end.

Output: a list of candidate clips, each defined by (start_segment_index,
end_segment_index). No phrase matching needed — the segment indices map
directly back to timestamps via transcript_cleanup.segments_for_range().

This step does ONE thing: find the boundaries. Scoring/ranking is Step 3.
"""
import json
import os
from typing import Any

from strands import Agent
from strands.models import BedrockModel

DETECTOR_MODEL = os.environ.get("BEDROCK_IDEATION_MODEL", "us.anthropic.claude-opus-4-6-v1")

SYSTEM = """You are a transcript analyst for a Vedantic philosophy podcast
(Bhagavad Gita) that teaches through modern analogies.

INPUT: A cleaned transcript where each line is:
  [N] text of segment N

YOUR JOB:
Identify 4-6 TOPIC SEGMENTS — stretches of consecutive segments where the
host makes one complete, self-contained teaching point.

A COMPLETE segment MUST include all three parts:
  1. The SETUP: a relatable modern problem or question
  2. The ANALOGY or SCRIPTURE reference (this is the pivot — NEVER cut mid-analogy)
  3. The LANDING: the practical takeaway restated in plain language

REJECT a segment if:
  - It ends mid-analogy (e.g., "the rope and the snake..." without the resolution)
  - It's pure Q&A with no teaching arc
  - It references "as I said earlier" (depends on previous context)
  - It's only scripture recitation without explanation
  - It ends mid-sentence or mid-comparison

PREFER segments where the host says something a 25-year-old dealing with
anxiety, comparison, or imposter syndrome would screenshot.

Duration: 20-120 seconds (roughly 6-35 segments). Let topic completeness
dictate length.

Return a JSON array of objects, each with:
  start_seg: integer — the [N] index where this topic begins
  end_seg: integer — the [N] index where this topic ends (INCLUSIVE)
  topic: string — 1-sentence summary of what the host is saying

Rules:
  - Topics must NOT overlap
  - Prefer segments with analogies, stories, or reframes
  - If two good topics are adjacent, keep them separate (don't merge)
  - Skip intros, tangents, admin talk
  - end_seg MUST be after the host's LANDING — the practical takeaway.
    If the last segment is mid-analogy, extend end_seg until the analogy
    resolves and the takeaway is stated.

Return ONLY the JSON array, no prose."""


def detect_segments(clean_text: str) -> list[dict[str, Any]]:
    """Return a list of {start_seg, end_seg, topic} dicts."""
    agent = Agent(
        model=BedrockModel(model_id=DETECTOR_MODEL, temperature=0.5),
        system_prompt=SYSTEM,
    )
    result = agent(
        f"Identify 4-6 self-contained topic segments in this transcript.\n\n"
        f"{clean_text}"
    )
    # Parse the JSON array from the response.
    text = str(result)
    # Strip markdown fences if present.
    if "```" in text:
        import re
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", text, re.DOTALL)
        if m:
            text = m.group(1)
    start = text.find("[")
    end = text.rfind("]")
    if start >= 0 and end > start:
        return json.loads(text[start : end + 1])
    raise ValueError(f"Could not parse segment detector output: {text[:300]}")
