"""Agent 2 (Strands): splits a continuous podcast window into scenes.

The idea's window is one uninterrupted stretch of the host talking. The
screenwriter cuts it into 4-6 scenes at natural sentence boundaries, assigns
visuals and on-screen text, and ensures scenes are back-to-back with no gaps.
"""
import json
import os

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

SCRIPT_MODEL = os.environ.get("BEDROCK_SCRIPT_MODEL", "us.anthropic.claude-sonnet-4-6")

SYSTEM = """You are a short-form video screenwriter for a Bhagavad Gita podcast.

Audience: 15-35 year olds, globally. Modern, relatable tone — not a sermon.

INPUT:
You receive a chosen IDEA with a continuous audio window:
  window_start, window_end — the bounds in the source podcast (seconds)
  window_text — the verbatim transcript of that window

You also receive the full timed transcript so you can find the exact segment
boundaries within the window.

YOUR JOB:
Split the window into scenes at natural sentence or clause boundaries.
Each scene's voiceover is a verbatim sub-slice of the window.

The reel length = the window length. If the window is 80 seconds, the reel
is 80 seconds. Do NOT shorten it.

CONTINUITY RULES (most important — non-negotiable):
1. Scenes are SEQUENTIAL and BACK-TO-BACK within the window.
   scene[0].source_start = window_start (or very close, within 0.5s)
   scene[i].source_start = scene[i-1].source_end
   scene[-1].source_end = window_end (or very close, within 0.5s)
   NO GAPS between scenes. NO JUMPS to a different part of the podcast.
2. Each scene's voiceover = verbatim text from source_start to source_end.
   Character-for-character from the transcript. No trimming, no reordering.
3. Per-scene source span: target 5-12 seconds. Max 15 seconds if the host is
   mid-sentence and splitting would sound unnatural.
   Roughly 1 scene per 8-12 seconds of audio:
     30s window → 3-5 scenes
     60s window → 5-8 scenes
     90s window → 8-12 scenes
4. Reel timeline must equal source timeline:
   scene.end - scene.start == scene.source_end - scene.source_start

Scene 1 MUST be the hook — the window was chosen because it opens strong.

- on_screen_text: SHORT (2-5 words), shouted-capitals, for silent autoplay.
- visual: one-sentence CINEMATIC scene description for AI video generation.
  Write it as a camera direction: "Slow zoom into a single candle flame
  flickering in a dark room" not "candle flame". Be specific about:
    • Camera motion (slow zoom, pull back, tracking shot, static close-up)
    • Subject (what we see)
    • Mood/lighting (golden hour, dark moody, misty, ethereal)
    • Motion (slow motion, time-lapse, still)
  This feeds directly to Amazon Nova Reel — the more cinematic the
  description, the better the generated video.

B-ROLL QUERIES — 3 ordered alternates per scene.
Think in METAPHORS not literal nouns. The podcast is spiritual/philosophical.
  ✅ "candle flame close up dark", "slow motion waves ocean", "mist forest",
     "ink drop water", "starfield cosmos", "rain window night", "empty road dawn"
  ❌ "hindu temple", "person praying", "man drinking alcohol", "brain on fire"
Topic-to-visual mappings:
  - ego / letting go → flowing water, wind, waves, falling leaves
  - ignorance / delusion → fog, mist, frosted glass, underwater light
  - clarity / awakening → sunrise, first light, lens flare, dawn mountains
  - action / free will → hands working, footsteps, runner slow-mo
  - cosmic / atman → starfield, nebula, night sky
  - anxiety → city rush time-lapse, flashing signs
  - peace → still pond, snowfall, steady flame
Also set `broll_query` = `broll_queries[0]` for back-compat.

- Caption: 1-2 sentences + call to action. Hashtags: 5-8 mixing broad
  (#Wisdom, #SelfGrowth) and niche (#BhagavadGita, #GitaWisdom)."""

REVISE_SYSTEM = (
    SYSTEM
    + "\n\nYou are revising an existing screenplay. Apply the user's instruction"
    " minimally — change only what they asked for, keep everything else intact."
    " Maintain CONTINUITY: scenes must still be sequential back-to-back within"
    " the original window. Return the FULL updated screenplay."
)


def _agent(system: str, temperature: float) -> Agent:
    return Agent(
        model=BedrockModel(model_id=SCRIPT_MODEL, temperature=temperature),
        system_prompt=system,
    )


def write_script(idea: dict, timed_transcript: str) -> Screenplay:
    prompt = (
        f"Split this idea's continuous window into scenes.\n\n"
        f"Idea:\n{json.dumps(idea, indent=2)}\n\n"
        f"Full timed transcript (find the window's segment boundaries here):\n"
        f"{timed_transcript}"
    )
    return _agent(SYSTEM, 0.3).structured_output(Screenplay, prompt)


def revise_script(screenplay: Screenplay, instruction: str) -> Screenplay:
    prompt = (
        f"Current screenplay:\n{screenplay.model_dump_json(indent=2)}\n\n"
        f"Revision instruction: {instruction}"
    )
    return _agent(REVISE_SYSTEM, 0.3).structured_output(Screenplay, prompt)
