"""LLM-as-Judge: evaluates a post-Visual-Director screenplay for quality.

Scores three dimensions (each 1-10):
  - visual_quality   : literal, camera-ready prompts; no banned abstract words
  - alignment        : each shot visual matches what the host is saying
  - house_style      : warm amber palette, shallow DOF, slow motion consistent

Returns a dict with overall_score, per-dimension scores, strengths,
improvements, and a verdict ("STRONG" / "GOOD" / "NEEDS_WORK").

Uses Haiku 4.5 (~$0.005/call) — fast and cheap enough to run on every short.
"""
import json
import os
import re

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

JUDGE_MODEL = os.environ.get(
    "BEDROCK_DIRECTOR_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SYSTEM = """You are a quality-control reviewer for AI-generated short video screenplays.
Your job is to evaluate each screenplay against strict production rules and
return a structured JSON report. Be specific and actionable — vague feedback
is useless.

================================================================
VISUAL QUALITY RULES (score this dimension)
================================================================
Every shot's `visual` field must:
- Describe something a camera crew could PHYSICALLY FILM
- Follow the format: [SHOT TYPE] of [SUBJECT doing ACTION] in [ENVIRONMENT],
  [LIGHTING], [LENS / DEPTH OF FIELD], [CAMERA MOTION], [COLOR GRADE]
- NEVER use these banned abstract words:
  metaphorical, symbolic, surreal, abstract, contemplative, spiritual,
  dreamlike, ethereal, meditative, transcendent, cosmic, infinite,
  otherworldly, divine, mystical
- NEVER use negation: no, not, without, never, avoid
- NEVER describe emotions or concepts as subjects ("loneliness sits by a window")
  — only physical objects and people doing physical actions

Deduct points for:
- Any banned word present (-1 per occurrence)
- Missing format elements (-1 per shot)
- Non-filmable concepts as subjects (-2 per shot)
- Negation in any shot (-1 per occurrence)

================================================================
VOICEOVER ALIGNMENT RULES (score this dimension)
================================================================
Each shot's visual must show WHAT THE HOST IS SAYING, not a thematic substitute.

CORRECT: VO "a bright person drinks alcohol" → shot of a hand lifting a glass
WRONG:   VO "a bright person drinks alcohol" → shot of flowers blooming

Deduct points for:
- Shot shows something thematically related but factually different from VO (-2 per shot)
- Shot is entirely disconnected from VO content (-3 per shot)

================================================================
HOUSE STYLE RULES (score this dimension)
================================================================
All shots must share a consistent visual language:
- Color grade: warm amber, golden, earthy — NOT cold blue or clinical white
- Lens: shallow depth of field, subject sharp, background soft — 35mm feel
- Lighting: soft and directional — golden hour, warm tungsten, candle, oil lamp
- Motion: slow and deliberate — slow push-in, dolly, pan, static. No fast cuts
- Texture: subtle film grain, cinematic

Deduct points for:
- Cold/clinical color grade (-1 per shot)
- Harsh flat lighting described (-1 per shot)
- Fast/frenetic camera motion described (-1 per shot)
- Inconsistent style between shots in the same beat (-1 per beat)

================================================================
OUTPUT FORMAT
================================================================
Return ONLY valid JSON, no prose before or after:

{
  "overall_score": <float 1-10, weighted: visual 40% + alignment 35% + house_style 25%>,
  "visual_quality_score": <float 1-10>,
  "alignment_score": <float 1-10>,
  "house_style_score": <float 1-10>,
  "strengths": [<2-3 specific things done well, concrete>],
  "improvements": [<2-3 specific actionable fixes, cite beat/shot numbers>],
  "verdict": <"STRONG" if overall>=8 | "GOOD" if overall>=6 | "NEEDS_WORK">
}"""


def judge_screenplay(screenplay: Screenplay) -> dict:
    """Evaluate a post-Visual-Director screenplay. Returns a quality report dict."""

    # Build lean input — only voiceover + shot visuals (strips broll_queries,
    # framing metadata etc.) to stay within Haiku's context efficiently.
    lean_beats = []
    for i, beat in enumerate(screenplay.beats):
        lean_shots = [
            {"shot": j + 1, "visual": s.visual}
            for j, s in enumerate(beat.shots)
        ]
        lean_beats.append({
            "beat": i + 1,
            "purpose": beat.purpose,
            "voiceover": beat.voiceover,
            "shots": lean_shots,
        })

    prompt = (
        "Evaluate this screenplay and return your JSON quality report.\n\n"
        + json.dumps(lean_beats, indent=2)
    )

    agent = Agent(
        model=BedrockModel(model_id=JUDGE_MODEL, temperature=0.1),
        system_prompt=SYSTEM,
    )
    result = str(agent(prompt))

    # Parse JSON — strip markdown fences if present.
    if "```" in result:
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", result, re.DOTALL)
        if m:
            result = m.group(1)
    start = result.find("{")
    end = result.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(result[start: end + 1])
        # Ensure verdict is always set.
        if "verdict" not in parsed:
            score = parsed.get("overall_score", 5)
            parsed["verdict"] = "STRONG" if score >= 8 else "GOOD" if score >= 6 else "NEEDS_WORK"
        return parsed

    raise ValueError(f"Could not parse judge output: {result[:400]}")
