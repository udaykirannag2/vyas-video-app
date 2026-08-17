"""Per-shot video quality scorer with prompt rewriter.

After Nova Reel generates a 6-second clip we extract a mid-clip JPEG frame
and send it to Claude Haiku (Bedrock vision) with the original visual prompt
and the beat's voiceover text. The scorer returns two independent signals:

  score          — overall cinematic quality (1-10)
  alignment_score — how well the visual matches the voiceover (1-10)

Retry logic in broll.py:
  - score < RETRY_THRESHOLD (6)         → retry with improved_prompt
  - alignment_score < ALIGNMENT_THRESHOLD (4) → retry even if score is fine,
    using an alignment-anchored prompt that shows what the host is saying

Cost: ~$0.001 per shot (Haiku vision call).
Runs only on Nova-generated primary shots, not Pexels clips.
"""
import base64
import json
import os
import re

import boto3

SCORER_MODEL = os.environ.get(
    "BEDROCK_SHOT_SCORER_MODEL",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
)
# Retry Nova Reel if overall score < this threshold. 0 disables retry.
RETRY_THRESHOLD = int(os.environ.get("SHOT_SCORE_RETRY_THRESHOLD", "6"))
# Retry if audio-video alignment score < this, regardless of overall score.
ALIGNMENT_THRESHOLD = int(os.environ.get("SHOT_ALIGNMENT_THRESHOLD", "4"))

_bedrock = boto3.client("bedrock-runtime")

SYSTEM = """You are a cinematography quality reviewer for AI-generated video clips.
You are shown a single frame extracted from a 6-second Nova Reel generated clip,
together with:
  - The visual prompt that was used to generate it
  - The voiceover the host speaks during this shot

Evaluate TWO things independently:

═══ 1. OVERALL CINEMATIC QUALITY (score 1-10) ════════════════
10 — Perfect: literal, clearly filmable, technically excellent
7-9 — Good: usable, only minor issues
5-6 — Passable but clear problems worth fixing on retry
3-4 — Poor: wrong subject, blurry, or contradicts voiceover
1-2 — Unusable: abstract fog/particles, blank, or completely wrong

Deduct points for:
  - Abstract fog, smoke, particles, or undefined shapes (-3)
  - Wrong subject (prompt says "man writing" but shows a landscape) (-3)
  - Blurry, overexposed, or heavy visual artifacts (-2)
  - The prompt's named subject is not visible in the frame (-2)

PORTRAIT FRAMING (this is a 9:16 vertical video — framing matters):
  - Human head or face cut off at the top edge of frame (-3)
  - Face partially cropped at left/right edge (-2)
  - Subject so close that only part of the body is visible when a wider
    shot was intended (-1)
  - Subject placement looks accidental — too extreme top/bottom (-1)

When a framing issue is detected, the improved_prompt MUST include
explicit head-room guidance, e.g.:
  "… framed from waist up with full head visible, subject centred
   vertically in the frame, generous headroom above …"

═══ 2. AUDIO-VIDEO ALIGNMENT (alignment_score 1-10) ══════════
This is independent of visual quality. It answers: "If a viewer
watches this clip while hearing the voiceover, does the visual
illustrate what the host is saying?"

10 — Perfect match: visual directly shows the topic being discussed
7-9 — Good: clearly related, viewer won't be confused
5-6 — Loose connection: thematically related but not literal
3-4 — Weak: visual is unrelated or contradicts the voiceover
1-2 — Completely wrong: visual actively misleads the viewer

Examples:
  Voiceover "most people check their phone 150 times a day"
  → Good: person looking at phone (8)
  → Poor: serene mountain landscape (2)

  Voiceover "meditation changes your brain structure"
  → Good: person sitting in meditation (9)
  → Poor: abstract blue particles (1)

═══ IMPROVED PROMPTS ═════════════════════════════════════════
When score < 7, write improved_prompt that:
  - Names SPECIFIC objects and SPECIFIC actions
  - Follows: [SHOT TYPE] of [SUBJECT doing ACTION] in [ENVIRONMENT],
    [LIGHTING], [LENS/DEPTH OF FIELD], [CAMERA MOTION], [COLOR GRADE]
  - Warm amber/golden colour grade, shallow DOF, slow deliberate motion
  - NEVER uses banned words: spiritual, metaphorical, ethereal, dreamlike,
    contemplative, abstract, transcendent, cosmic, mystical, divine
  - NEVER uses negation: no, not, without, avoid, never

When alignment_score < 5, write alignment_improved_prompt that:
  - MUST visually depict what the host is LITERALLY saying in the voiceover
  - Re-anchor the visual to the voiceover topic (e.g. if host says "phone
    addiction", show a person holding a phone, not abstract concepts)
  - Same format and style rules as improved_prompt above

Return ONLY valid JSON, no prose:
{
  "score": <int 1-10>,
  "feedback": "<one specific sentence about the main visual quality problem>",
  "improved_prompt": "<rewritten literal prompt — copy original unchanged if score >= 7>",
  "alignment_score": <int 1-10>,
  "alignment_note": "<one sentence: does the visual match the voiceover? What specifically is misaligned?>",
  "alignment_improved_prompt": "<prompt anchored to voiceover content — copy improved_prompt unchanged if alignment_score >= 5>"
}"""


def score_shot(
    frame_jpeg: bytes,
    visual_prompt: str,
    voiceover: str,
) -> dict:
    """Score a Nova Reel frame against the intended visual prompt and voiceover.

    Returns:
        dict with keys:
          score (int)                    — overall cinematic quality 1-10
          feedback (str)                 — main quality problem
          improved_prompt (str)          — rewritten prompt for quality retry
          alignment_score (int)          — audio-video alignment 1-10
          alignment_note (str)           — description of alignment issue
          alignment_improved_prompt (str)— prompt anchored to voiceover

    Never raises — falls back to score=7, alignment_score=7 (pass) on any
    error so the pipeline is never blocked by a scorer failure.
    """
    try:
        image_b64 = base64.standard_b64encode(frame_jpeg).decode()
        response = _bedrock.invoke_model(
            modelId=SCORER_MODEL,
            contentType="application/json",
            accept="application/json",
            body=json.dumps({
                "anthropic_version": "bedrock-2023-05-31",
                "max_tokens": 768,
                "system": SYSTEM,
                "messages": [{
                    "role": "user",
                    "content": [
                        {
                            "type": "image",
                            "source": {
                                "type": "base64",
                                "media_type": "image/jpeg",
                                "data": image_b64,
                            },
                        },
                        {
                            "type": "text",
                            "text": (
                                f"Visual prompt used to generate this clip:\n{visual_prompt}\n\n"
                                f"Voiceover spoken during this shot:\n{voiceover}\n\n"
                                "Score this frame on BOTH dimensions and return your JSON evaluation."
                            ),
                        },
                    ],
                }],
            }),
        )
        text = json.loads(response["body"].read())["content"][0]["text"]

        # Strip markdown fences if present.
        if "```" in text:
            m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
            if m:
                text = m.group(1)
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            parsed = json.loads(text[start:end + 1])
            parsed.setdefault("improved_prompt", visual_prompt)
            parsed.setdefault("alignment_score", 7)
            parsed.setdefault("alignment_note", "alignment not evaluated")
            parsed.setdefault("alignment_improved_prompt", parsed["improved_prompt"])
            return parsed

    except Exception as e:
        print(f"[shot_scorer] non-fatal error: {e!r}")

    # Fallback: treat as passing so the pipeline is never blocked.
    return {
        "score": 7,
        "feedback": "scorer unavailable",
        "improved_prompt": visual_prompt,
        "alignment_score": 7,
        "alignment_note": "scorer unavailable",
        "alignment_improved_prompt": visual_prompt,
    }
