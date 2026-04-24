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

   TITLES — write FOUR title variants. Return primary in `title`, then
   `alt_title_1` (question version), `alt_title_2` (contrast version),
   `hook_title` (first-line overlay for the reel cover).

   Title rules (applies to all four variants):
     • 4-9 words. High curiosity. Plain English.
     • Clear before clever. Understandable on first read.
     • START FROM THE EMOTIONAL TENSION, not the topic.
     • Choose ONE of these frames:
       1. direct question       — "Why Smart People Still Suffer"
       2. painful contradiction — "You're Not Tired, You're Scattered"
       3. hidden truth          — "The Real Reason Peace Feels Hard"
       4. everyday struggle     — "You Don't Need More Motivation"
       5. sharp reframe         — "Krishna's Fix for Overthinking"
     • Avoid generic words unless earned: truth, life, wisdom, spiritual,
       divine, soul, consciousness. Specific > abstract.
     • Avoid lecture/sermon tone. Avoid bland clip summaries.
     • Spiritual nouns OK when the clip is literally about them (Krishna,
       Gita, Karma, Ego, Dharma). Don't pad titles with them.
     • Make it relevant to: work, relationships, stress, ego, discipline,
       peace, overthinking, comparison, burnout.

   Good title shapes:
     "Why Smart People Still Suffer"
     "You're Not Tired, You're Scattered"
     "The Real Reason Peace Feels Hard"
     "Krishna's Fix for Overthinking"
     "You Don't Need More Motivation"
     "Mind vs. Intellect — Who's Actually Driving?"

   Variant guidance:
     - `title`: the strongest, most direct version.
     - `alt_title_1`: recast as a sharper question (even if primary is already a question, make this one punchier or more specific).
     - `alt_title_2`: recast as a contrast / contradiction ("X, not Y" or "It's not X, it's Y").
     - `hook_title`: the version that works as ON-SCREEN cover text (shouted-capitals-friendly, ≤6 words, very punchy).

   DESCRIPTION — reader-facing blurb for YouTube/Instagram caption:
     - Sentence 1: identify the tension in MODERN language (not spiritual jargon).
     - Sentence 2: hint at the insight or payoff — don't give it away.
     - Do NOT repeat the title word-for-word.
     - Do NOT sound preachy or hype-y.
     - End with a soft CTA (no exclamation marks, no "watch till the end").
     - 2 sentences total, ≤ 280 chars.

   BAD description examples (do not produce these):
     ❌ "In this profound spiritual reel we explore..."
     ❌ "Watch till the end for divine wisdom..."
     ❌ "This video explains an important Bhagavad Gita teaching..."

   GOOD description examples:
     ✅ "Most advice treats overthinking like a motivation problem. Krishna
        hands Arjuna a specific, unfashionable fix. Worth 90 seconds."
     ✅ "You know what to do. So why does it feel impossible to do it?
        The Gita's take is sharper than you'd expect."

   Other fields:
     - summary: 2-3 sentence description of the narrative arc (internal
       use, shown in the episode dashboard — NOT the same as description).
     - why_it_works: 1-2 sentences on audience resonance.
     - hook_line: the single most scroll-stopping line from the clip text.
       Copy EXACTLY verbatim. Becomes scene 1's on-screen text.
     - twist_line: the key insight / reframe moment. EXACT verbatim copy.
     - payoff_line: the conclusion / takeaway. EXACT verbatim copy.
     - verse_ref: which Bhagavad Gita verse this relates to.

3. RANK the selected clips: rank 1 = strongest reel.

Return a JSON array of the top 2-3 clips:
[
  {
    "clip_id": 0,
    "score": 9,
    "rank": 1,
    "title": "Why Smart People Still Suffer",
    "alt_title_1": "Why Can't Smart People Stop Overthinking?",
    "alt_title_2": "Smart Doesn't Mean Steady",
    "hook_title": "SMART ISN'T ENOUGH",
    "description": "Most overthinkers assume they need more clarity. Krishna points at the actual bottleneck — and it isn't intelligence. Worth 60 seconds.",
    "summary": "Host uses the drunk-genius analogy to show how knowledge gets covered by ego; lands on the idea that wisdom isn't gone, just buried.",
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
