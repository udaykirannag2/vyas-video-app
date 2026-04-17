"""Agent 2 (Strands): splits a continuous podcast window into BEATS, each with
2-4 visual SHOTS. The voiceover stays as one continuous verbatim audio slice
per beat, but the visual track cuts between multiple shots for edited-sequence
pacing."""
import json
import os

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

SCRIPT_MODEL = os.environ.get("BEDROCK_SCRIPT_MODEL", "us.anthropic.claude-sonnet-4-6")

SYSTEM = """You are a short-form video screenwriter for a Bhagavad Gita podcast.

Audience: 15-35 year olds, globally. Modern, relatable tone — not a sermon.

INPUT:
You receive a chosen IDEA with a continuous audio window (window_start,
window_end, window_text) and the full timed transcript.

OUTPUT:
A screenplay with BEATS (spoken segments) each containing 2-4 visual SHOTS.

STRUCTURE:
  reel
    beats[]          ← spoken audio segments (verbatim from the podcast)
      shots[]        ← visual clips that play DURING this beat's audio

BEAT RULES (spoken audio):
- Beats are SEQUENTIAL and BACK-TO-BACK within the window (no gaps, no jumps).
- Each beat's voiceover = verbatim text from source_start to source_end.
- Per-beat duration: target 5-15 seconds. Max 20s if mid-sentence.
- Each beat has a `purpose`: hook | setup | build | twist | payoff.
- `on_screen_text`: 2-5 words, CAPITALS, for silent autoplay.
- The pipeline rewrites beat.start/end to match source duration — don't
  worry about getting timeline math right. Focus on beats and shots.

SHOT RULES (visual clips inside a beat):
Each beat MUST have 2-4 shots. Each shot is a separate Nova Reel clip.

For each shot, provide:
  shot_number: 1, 2, 3... within the beat
  shot_duration_sec: how many seconds this shot holds (shots tile across the
    beat duration; they should roughly sum to the beat's source duration)
  shot_role: one of:
    hook       — pattern interrupt, grabs attention (beat 1 shot 1)
    establish  — sets the scene, grounds the viewer
    detail     — close-up, texture, specific element
    contrast   — juxtaposition, before/after, light vs dark
    payoff     — resolution, expansive calm
    reflection — contemplative pause, breathing room
  visual_mode: one of:
    literal    — concrete recognizable imagery (hands, water, candle)
    hybrid     — semi-abstract (ink in water, light through mist)
    metaphorical — fully abstract/symbolic (starfield, color gradient)
  visual: cinematic scene description for Nova Reel (camera + subject + mood)
  framing: close-up | medium | wide | extreme-close-up | aerial | detail
  camera_movement: slow zoom | static | tracking | pull back | dolly | drift
  transition_hint: cut | dissolve | match-cut | fade (how to enter this shot)
  broll_queries: 3 Pexels fallback search terms
  broll_query: first query (back-compat)

SHOT VARIATION within a beat:
- Vary framing: if shot 1 is wide, shot 2 should be close-up or detail.
- Vary visual_mode: literal → hybrid → metaphorical progression within a beat.
- Vary camera_movement: don't repeat the same motion 3 times.
- First shot of beat 1 (the hook) should be the most striking visual.
- Later beats can trend more metaphorical as the reel builds.

VISUAL PRINCIPLES (same as before):
- METAPHORS not literals. If VO says "car" → show flowing energy, not a car.
- Spiritual, contemplative, cinematic. "Terrence Malick meets Alan Watts."
- NO faces, NO religious symbols, NO text in frame.
- Each visual MUST include: camera motion, subject, lighting/mood.

Caption: 1-2 sentences + CTA. Hashtags: 5-8 broad + niche.

Return JSON with `beats` array (not `scenes`). Each beat has `shots` array."""

REVISE_SYSTEM = (
    SYSTEM
    + "\n\nYou are revising an existing screenplay. Apply the user's instruction"
    " minimally. Maintain continuity. Return the FULL updated screenplay."
)


def _agent(system: str, temperature: float) -> Agent:
    return Agent(
        model=BedrockModel(model_id=SCRIPT_MODEL, temperature=temperature),
        system_prompt=system,
    )


def write_script(idea: dict, timed_transcript: str) -> Screenplay:
    prompt = (
        f"Create a multi-shot screenplay for this idea.\n\n"
        f"Idea:\n{json.dumps(idea, indent=2)}\n\n"
        f"Source timed transcript:\n{timed_transcript}"
    )
    return _agent(SYSTEM, 0.3).structured_output(Screenplay, prompt)


def revise_script(screenplay: Screenplay, instruction: str) -> Screenplay:
    prompt = (
        f"Current screenplay:\n{screenplay.model_dump_json(indent=2)}\n\n"
        f"Revision instruction: {instruction}"
    )
    return _agent(REVISE_SYSTEM, 0.3).structured_output(Screenplay, prompt)
