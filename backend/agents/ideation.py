"""Agent 1 (Strands): reads plain text transcript and identifies the START
and END phrases of 2-3 reel-worthy topic passages. Code then extracts the
full passage and aligns to audio timestamps.

This two-pass design keeps the LLM focused on content understanding:
  - LLM identifies WHERE a topic begins and ends (semantic task)
  - Code extracts and aligns (mechanical task)
"""
import os

from strands import Agent
from strands.models import BedrockModel

from models import IdeasResponse

IDEATION_MODEL = os.environ.get("BEDROCK_IDEATION_MODEL", "us.anthropic.claude-opus-4-6-v1")

SYSTEM = """You are a short-form video ideation agent for a Bhagavad Gita podcast.

Your audience is 15-35 year olds, globally. Frame ideas in modern, relatable,
universally applicable ways — like a life-hack or a mindset reframe, not a
sermon.

INPUT: The plain text transcript of a podcast episode.

YOUR JOB:
Find 2 or 3 passages in the transcript that would each make a great
reel. The clip can be 30s to 180s — let the topic dictate length. For each, identify:

  1. `start_phrase` — the FIRST 6-10 words of the passage, copied exactly
     from the transcript. This is where the topic BEGINS (the hook/setup).

  2. `end_phrase` — the LAST 6-10 words of the passage, copied exactly
     from the transcript. This is where the topic's CONCLUSION/PAYOFF lands.

The code will extract everything between start_phrase and end_phrase from the
transcript. You do NOT need to copy the full passage — just identify its
boundaries by meaning.

RULES FOR PICKING BOUNDARIES:

start_phrase must be the beginning of the topic:
  ✅ "So a lot of times the misunderstanding is" — clear topic opener
  ✅ "Let's take the example of a bright person" — starts an analogy
  ❌ "Um, so, yeah" — filler, not a real start

end_phrase must be AFTER the topic's conclusion/payoff:
  ✅ "that is their nature, their swabhava, and you have yours" — conclusion
  ✅ "and that's the whole teaching of this verse" — wrap-up
  ❌ "tigers, lions are ferocious, and cows are" — mid-comparison, INCOMPLETE
  ❌ "but our knowledge is covered by" — mid-sentence, INCOMPLETE

The passage between start and end should be a COMPLETE MINI-TALK:
  - setup → development → payoff
  - Self-contained: someone who hears ONLY this clip understands the point
  - Ends AFTER the host lands the conclusion, not mid-analogy

Also return:
  - title: punchy reel title (max 60 chars)
  - target_length_sec: estimated spoken duration (let the topic dictate length)
  - rank: 1 = best

NARRATIVE ARC — for each idea, also identify these lines from the passage:
  - hook_line: the single most attention-grabbing line. This becomes the
    pattern-interrupt text overlaid on scene 1. Pick the boldest claim,
    sharpest question, or most counterintuitive statement.
  - twist_line: the key insight / reframe moment — "the aha." The moment
    the listener's mental model shifts.
  - payoff_line: the conclusion / takeaway — what the listener walks away
    with. Should feel like a resolution.
  Copy these EXACTLY from the transcript (verbatim).

LENGTH: Let the topic dictate the clip length (30-180 seconds). Complete
topics can be long — don't artificially shorten. HARD MAX: 180 seconds
(3 minutes — the limit for YouTube Shorts and Instagram Reels).

Set window_start, window_end to 0 (code fills them). Set window_text to ""
(code fills it). Set quotes to empty list (deprecated).

Put start_phrase and end_phrase inside the `hook` and `summary` fields
respectively using this format:
  hook: "START_PHRASE: <the exact start phrase>"
  summary: "END_PHRASE: <the exact end phrase> ||| <your actual summary>"

This encoding lets us parse them without changing the schema."""


def _build_agent() -> Agent:
    return Agent(
        model=BedrockModel(model_id=IDEATION_MODEL, temperature=0.7),
        system_prompt=SYSTEM,
    )


def generate_ideas(plain_transcript: str) -> IdeasResponse:
    agent = _build_agent()
    result = agent.structured_output(
        IdeasResponse,
        f"Find 2-3 reel-worthy topic passages. For each, identify the start "
        f"and end phrases (boundaries) of the complete topic.\n\n"
        f"Transcript:\n\n{plain_transcript}",
    )
    return result


def parse_phrases(idea_dict: dict) -> tuple[str, str, str]:
    """Extract start_phrase, end_phrase, and clean summary from the encoded fields."""
    hook = idea_dict.get("hook", "")
    summary = idea_dict.get("summary", "")

    start_phrase = ""
    if "START_PHRASE:" in hook:
        start_phrase = hook.split("START_PHRASE:", 1)[1].strip().strip('"').strip()
    else:
        # Fallback: use hook as-is (first few words)
        start_phrase = hook.strip()

    end_phrase = ""
    clean_summary = summary
    if "END_PHRASE:" in summary:
        parts = summary.split("END_PHRASE:", 1)[1]
        if "|||" in parts:
            end_phrase, clean_summary = parts.split("|||", 1)
        else:
            end_phrase = parts
        end_phrase = end_phrase.strip().strip('"').strip()
        clean_summary = clean_summary.strip()

    return start_phrase, end_phrase, clean_summary
