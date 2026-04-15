"""Agent 2 (Strands): turns a chosen idea (with its pre-picked verbatim quotes)
into a scene-by-scene screenplay. Every scene's voiceover is a verbatim slice
of the source podcast — no trimming, no paraphrasing."""
import json
import os

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

SCRIPT_MODEL = os.environ.get("BEDROCK_SCRIPT_MODEL", "us.anthropic.claude-sonnet-4-6")

SYSTEM = """You are a short-form video screenwriter for a Bhagavad Gita podcast.

Audience: 15-35 year olds, globally. Modern, relatable tone — not a sermon.

REEL VOICEOVER SOURCE — STRICT VERBATIM (most important rule):
The reel will be voiced by the podcast host using slices of the original recording.
You are given:
  - the chosen idea, including its pre-picked `quotes` (start_sec, end_sec, text)
  - the podcast as TIMED SEGMENTS — one per line: (start_sec-end_sec) text

Every scene's `voiceover` MUST be a VERBATIM concatenation of one or more CONSECUTIVE timed
segments. Rules are strict:
  - Preferentially use the idea's pre-picked quotes as scene voiceovers (one quote per scene
    is the common case).
  - If you need more scenes than there are quotes, pick additional contiguous spans directly
    from the timed transcript.
  - Set source_start = first segment's start_sec; source_end = last segment's end_sec.
  - `voiceover` text MUST match the transcript exactly — character-for-character.
  - NEVER trim, drop, or reorder words inside a span. Not even filler ("um", "you know").
    If a span has filler you dislike, either (a) pick a shorter sub-span that starts/ends at
    a cleaner segment boundary, or (b) pick a different span. Do NOT edit the text.
  - NEVER invent sentences, metaphors, or examples.

If you cannot find a clean verbatim span for a scene, make the reel SHORTER (fewer scenes).
A short authentic reel is better than a padded one with invented content.

Output a scene-by-scene screenplay for a vertical 9:16 reel. Rules:
- Total duration 20-40 seconds. Scenes are typically 2-6 seconds each.
- Scene 1 MUST be the hook — use the punchiest available quote.
- scene `start` and `end` are POSITIONS IN THE REEL (timeline), flowing 0 → duration_sec
  with no gaps.
- scene `source_start` and `source_end` are POSITIONS IN THE SOURCE PODCAST (required).
- on_screen_text: SHORT (2-5 words), shouted-capitals, for silent autoplay viewers.
- visual: one-sentence b-roll direction.

B-ROLL QUERIES — this matters; read carefully.
The podcast is spiritual / philosophical. Most scenes are ABSTRACT or METAPHORICAL,
not literal. Pexels / stock-video search rewards cinematic nature and atmospheric
footage. Write `broll_queries` (exactly 3, ordered most-preferred first) that will
retrieve footage matching the MOOD of the voiceover, not a literal object in it.

Think in metaphors, not nouns:
  ✅ GOOD — "candle flame close up", "slow motion waves ocean", "mist forest morning",
     "starfield cosmos slow", "rain window night", "wind long grass", "empty road dawn",
     "ink drop water slow motion", "silhouette walking fog", "city lights night rain",
     "single leaf floating stream", "golden hour field", "fire embers dark".
  ❌ BAD — literal or on-the-nose: "man drinking alcohol", "Hindu temple", "God in sky",
     "person praying", "brain on fire", "devotee meditating Ganga".

Specific mappings you should reach for:
  - ego / attachment / letting go → flowing water, wind, waves, falling leaves
  - confusion / delusion / ignorance → fog, mist, frosted glass, underwater light
  - clarity / realisation / awakening → sunrise, first light, opening eyes macro,
    lens flare, dawn over mountains
  - action / duty / free will → hands working, footsteps, runner slow motion, machinery
  - cosmic / atman / universal energy → starfield, nebula, night sky, spinning galaxy
  - anxiety / overthinking → city rush time-lapse, flashing signs, close-up strained eyes
  - stillness / peace → single candle, still pond, snowfall, steady flame

AVOID:
  - Religious iconography (temples, idols, priests, rituals, puja, saffron robes) —
    it dates the reel and narrows the audience. Use universal nature/atmosphere.
  - Indian-specific visual shorthand unless the line itself is literally about India.
  - Cheesy wellness clichés (silhouette on mountaintop sunset, hands in prayer).
  - Clips where a human face would dominate the frame during a contemplative line —
    prefer hands, landscapes, objects, or slow motion abstracts.

Each query: 2-5 Pexels-friendly keywords. Be concrete about texture, motion, light,
and framing ("close up", "slow motion", "aerial", "macro") because those words are
what stock libraries tag on.

Also fill `broll_query` with the same text as `broll_queries[0]` for back-compat.

- Caption: 1-2 sentences + call to action. Hashtags: 5-8 mixing broad (#Wisdom,
  #SelfGrowth) and niche (#BhagavadGita, #GitaWisdom)."""

REVISE_SYSTEM = (
    SYSTEM
    + "\n\nYou are revising an existing screenplay. Apply the user's instruction minimally —"
    " change only what they asked for, keep everything else intact, and continue to respect"
    " the STRICT VERBATIM rule. Return the FULL updated screenplay."
)


def _agent(system: str, temperature: float) -> Agent:
    return Agent(
        model=BedrockModel(model_id=SCRIPT_MODEL, temperature=temperature),
        system_prompt=system,
    )


def write_script(idea: dict, timed_transcript: str) -> Screenplay:
    prompt = (
        f"Write the screenplay for this chosen idea.\n\n"
        f"Idea (including pre-picked verbatim quotes):\n{json.dumps(idea, indent=2)}\n\n"
        f"Source timed transcript (verbatim source of truth):\n{timed_transcript}"
    )
    return _agent(SYSTEM, 0.3).structured_output(Screenplay, prompt)


def revise_script(screenplay: Screenplay, instruction: str) -> Screenplay:
    prompt = (
        f"Current screenplay:\n{screenplay.model_dump_json(indent=2)}\n\n"
        f"Revision instruction: {instruction}"
    )
    return _agent(REVISE_SYSTEM, 0.3).structured_output(Screenplay, prompt)
