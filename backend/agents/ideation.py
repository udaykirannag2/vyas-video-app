"""Agent 1 (Strands): scans the timed podcast transcript and identifies 2-3
continuous windows (20-40s each) that each tell a complete story suitable for
a short-form reel. Each window is one uninterrupted stretch of the host
talking — no jumping around the episode."""
import os

from strands import Agent
from strands.models import BedrockModel

from models import IdeasResponse

IDEATION_MODEL = os.environ.get("BEDROCK_IDEATION_MODEL", "us.anthropic.claude-opus-4-6-v1")

SYSTEM = """You are a short-form video ideation agent for a Bhagavad Gita podcast.

Your audience is 15-35 year olds, globally. Frame ideas in modern, relatable,
universally applicable ways — like a life-hack or a mindset reframe, not a
sermon.

INPUT FORMAT:
You are given the podcast as TIMED SEGMENTS — one per line:
  (start_sec-end_sec) text

YOUR JOB:
Find 2 or 3 CONTINUOUS WINDOWS in the podcast, each suitable for one reel.

A "continuous window" is a stretch of consecutive timed segments where the host
makes ONE coherent point from start to finish — a setup, a development, and a
payoff. Think of it as a clip you'd trim from the full episode.

For each idea:
  - `window_start` = start_sec of the first segment in the window
  - `window_end` = end_sec of the last segment in the window
  - `window_text` = verbatim concatenation of all segments in the window
  - `target_length_sec` = window_end - window_start (should be 20-40 seconds)

RULES:
- Window must be 20-40 seconds (sum of segment durations). 25-35s is the sweet
  spot for short-form.
- Window must be CONTINUOUS — consecutive segments with no gap. Do NOT cherry-
  pick segments from different parts of the episode.
- Window must tell a COMPLETE micro-story: the listener should get the point
  even without the rest of the episode. Look for:
    - a provocative claim → evidence/analogy → takeaway
    - a question → explanation → punch line
    - a relatable problem → reframe → practical advice
- The HOOK (first 3 seconds of the window) must be a pattern interrupt —
  a question, a bold claim, or a counterintuitive statement.
- Prefer framings like: anxiety, procrastination, burnout, social comparison,
  failure, relationships, decision paralysis.
- Do NOT use heavy Sanskrit jargon without an immediate plain-English gloss.
- `quotes` field: leave as an empty list (deprecated — `window_*` fields
  replace it).
- Rank by expected youth resonance (rank 1 = best)."""


def _build_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=IDEATION_MODEL, temperature=0.8),
        system_prompt=SYSTEM,
    )


def generate_ideas(timed_transcript: str) -> IdeasResponse:
    agent = _build_agent()
    result = agent.structured_output(
        IdeasResponse,
        f"Find 2-3 continuous windows in this podcast, each suitable for a reel.\n\n"
        f"Timed transcript:\n\n{timed_transcript}",
    )
    return result
