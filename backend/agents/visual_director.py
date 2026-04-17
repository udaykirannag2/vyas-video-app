"""Agent 3 (Strands, Haiku 4.5): polishes the visual shots across all beats.

Now operates on SHOTS inside beats, not just one visual per scene. Enforces:
- Shot-to-shot framing variation within each beat
- Beat-to-beat emotional arc progression
- Nova Reel-optimized cinematic descriptions
- Pexels fallback queries
"""
import os

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

DIRECTOR_MODEL = os.environ.get(
    "BEDROCK_DIRECTOR_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SYSTEM = """You are a cinematographer polishing visual shots for AI video generation.

The screenplay has BEATS (spoken segments) each containing 2-4 SHOTS.

Your ONE job: rewrite each shot's `visual`, `framing`, `camera_movement`,
`transition_hint`, and `broll_queries` fields. Do NOT change any other field
(voiceover, source timestamps, purpose, shot_role, shot_number, on_screen_text).

EMOTIONAL ARC — use each beat's `purpose` to set the register:
  hook:   Unexpected. High contrast. Tight framing. Dark-to-light.
  setup:  Grounding. Warm. Wide. Familiar.
  build:  Tension. Movement increasing. Shadows growing.
  twist:  Transformation. Light burst. Dramatic reveal.
  payoff: Resolution. Expansive. Calm. Golden hour.

SHOT-LEVEL POLISH:
For each shot's `visual` field, write a CINEMATIC CAMERA DIRECTION:
  - Camera motion + subject + lighting/mood + motion quality
  - Spiritual, contemplative, dreamlike. "Terrence Malick meets Alan Watts."
  - No faces, no religious symbols, no text in frame.
  - Metaphorical > literal (if VO says "car" → show energy flow, not a car)

VARIATION WITHIN A BEAT (critical for edited-sequence feel):
  - Shot 1 wide → shot 2 close-up → shot 3 detail (vary scale!)
  - Don't repeat the same framing in consecutive shots
  - First shot of a beat establishes; last shot transitions
  - `transition_hint`: use dissolve for contemplative beats, cut for energetic,
    match-cut for visual rhymes between beats

VARIATION ACROSS BEATS:
  - Early beats (hook/setup): tighter, darker, more literal
  - Later beats (twist/payoff): wider, brighter, more metaphorical
  - The reel should feel like a visual JOURNEY, not 12 similar clips

Each shot also needs `broll_queries` (3 Pexels fallback keywords) and
`broll_query` = first query.

Return the FULL screenplay JSON with only shot visual fields changed."""


def direct(screenplay: Screenplay) -> Screenplay:
    agent = Agent(
        model=BedrockModel(model_id=DIRECTOR_MODEL, temperature=0.5),
        system_prompt=SYSTEM,
    )
    prompt = (
        "Polish the visual shots in this screenplay.\n"
        "Keep every non-visual field identical.\n\n"
        + screenplay.model_dump_json(indent=2)
    )
    return agent.structured_output(Screenplay, prompt)
