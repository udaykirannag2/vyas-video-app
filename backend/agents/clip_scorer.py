"""Step 3 of ideation: candidate clip scoring.

Takes the candidate topic segments (from Step 2) with their full text, and
scores them for reel-worthiness. Picks the top 2-3 and annotates each with:
  - title, summary, hook_line, twist_line, payoff_line
  - why_it_works (audience resonance)
  - rank

This step does SCORING and NARRATIVE ARC annotation. It does NOT do
boundary detection (that was Step 2) or timestamp alignment (that's code).
"""
import os

from strands import Agent
from strands.models import BedrockModel

# Uses a slightly cheaper model since this is scoring, not deep analysis.
SCORER_MODEL = os.environ.get("BEDROCK_SCRIPT_MODEL", "us.anthropic.claude-sonnet-4-6")

SYSTEM = """You are a reel content strategist for a Bhagavad Gita podcast.
Audience: 15-35 year olds globally. Modern, relatable, not preachy.

INPUT: A list of candidate clips extracted from a podcast episode. Each clip
has an ID, topic summary, and the full verbatim text.

YOUR JOB:
1. SCORE each clip for reel-worthiness (1-10). Use these weighted criteria:

   Emotional resonance (30%):
     Does it hit anxiety, comparison, imposter syndrome, procrastination,
     burnout, relationships, or decision paralysis? Would a 25-year-old
     screenshot this and send to a friend?

   Hook strength (25%):
     Does it open with something that stops a scroll? A bold claim, a
     provocative question, a counterintuitive statement?

   Insight / reframe (25%):
     Is there an "I never thought of it that way" moment? A clear pivot
     where the host flips the listener's assumption?

   Payoff clarity (15%):
     Does it end with a concrete takeaway — something the listener can
     DO or THINK differently starting tonight?

   Self-containment (5%):
     Does the clip make sense without hearing the rest of the episode?
     (Should always be true if the segment detector did its job.)

2. For the TOP 2-3 clips (score ≥ 7), annotate:
   - title: reel title, max 65 characters. Rules:
     • Clear before clever — understandable on first read
     • Use familiar spiritual nouns when present: Krishna, Gita, Namaste,
       Pandavas, Mind, Intellect, Karma, Ego, Dharma
     • Prefer a question, contrast, or "true meaning" framing
     • Avoid overly poetic metaphor unless the metaphor IS the clip's subject
     • If the clip explains a known concept, title the concept directly
     • If the clip resolves confusion, use a question title
     Style examples:
       "The True Meaning of NAMASTE"
       "Mind vs. Intellect — Who's Really in Control?"
       "Why Are There Exactly 5 Pandavas?"
       "Why Krishna Told Arjuna to Fight"
       "What Karma Really Means"
       "Ego vs. Self — What's the Difference?"
       "Why the Gita Doesn't Teach Escape"
   - summary: 2-3 sentence description of the arc
   - why_it_works: 1-2 sentences on audience resonance
   - hook_line: the single most scroll-stopping line from the clip text.
     Copy it EXACTLY verbatim. This becomes the pattern-interrupt text on scene 1.
   - twist_line: the key insight / reframe moment. EXACT verbatim copy.
   - payoff_line: the conclusion / takeaway. EXACT verbatim copy.
   - verse_ref: which Bhagavad Gita verse this relates to

3. RANK the selected clips: rank 1 = strongest reel.

Return a JSON array of the top 2-3 clips:
[
  {
    "clip_id": 0,
    "score": 9,
    "rank": 1,
    "title": "...",
    "summary": "...",
    "why_it_works": "...",
    "hook_line": "...",
    "twist_line": "...",
    "payoff_line": "...",
    "verse_ref": "BG X.Y"
  }
]

Return ONLY the JSON array, no prose."""


def score_clips(candidates: list[dict]) -> list[dict]:
    """Score and annotate candidate clips. Returns top 2-3 with arc annotations."""
    agent = Agent(
        model=BedrockModel(model_id=SCORER_MODEL, temperature=0.5),
        system_prompt=SYSTEM,
    )
    import json as _json
    import re

    prompt = "Score these candidate clips and return the top 2-3 with annotations.\n\n"
    for i, c in enumerate(candidates):
        prompt += f"--- Clip {i} ---\n"
        prompt += f"Topic: {c['topic']}\n"
        prompt += f"Duration: ~{c.get('duration_sec', '?')}s\n"
        prompt += f"Text:\n{c['text']}\n\n"

    result = str(agent(prompt))
    # Parse JSON array.
    if "```" in result:
        m = re.search(r"```(?:json)?\s*(\[.*?\])\s*```", result, re.DOTALL)
        if m:
            result = m.group(1)
    start = result.find("[")
    end = result.rfind("]")
    if start >= 0 and end > start:
        return _json.loads(result[start : end + 1])
    raise ValueError(f"Could not parse scorer output: {result[:300]}")
