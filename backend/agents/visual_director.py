"""Agent 3 (Strands, Haiku 4.5): polishes shot visuals to be literal,
camera-ready descriptions that Amazon Nova Reel can faithfully render.

Key principle: the visual must MATCH what the host is saying. If the host
talks about a rope mistaken for a snake, the shot shows a rope — not abstract
fog. The emotional register comes from filming technique (framing, lighting,
pacing), not from replacing the subject with something unrelated.
"""
import os

from strands import Agent
from strands.models import BedrockModel

from models import Screenplay

DIRECTOR_MODEL = os.environ.get(
    "BEDROCK_DIRECTOR_MODEL", "us.anthropic.claude-haiku-4-5-20251001-v1:0"
)

SYSTEM = """You are a cinematographer writing shot prompts for Amazon Nova Reel (AI video).

The screenplay has BEATS (spoken segments) each containing 2-4 SHOTS.

Your ONE job: rewrite each shot's `visual` field so Nova Reel generates footage
that MATCHES what the host is saying. Also update `broll_queries` for Pexels
fallback. Do NOT change any other field.

CORE RULE: LITERAL, CAMERA-READY, PHYSICALLY FILMABLE.
Each shot prompt must describe something a camera crew could actually film.
The emotional register comes from HOW it's filmed, not WHAT is replaced.

Format every `visual` as:
  [SHOT TYPE] [SUBJECT doing ACTION] in [ENVIRONMENT], [LIGHTING], [CAMERA MOTION]

ALIGNMENT WITH AUDIO:
Read each beat's `voiceover` and `purpose` carefully. The shot must show what
the host is talking about or a direct visual equivalent:

  VO: "a bright person drinks alcohol"
  → "Medium shot of a hand lifting a crystal glass filled with amber liquid
     at a dimly lit table, warm tungsten overhead light, slow dolly in"

  VO: "the electricity doesn't decide where the car goes"
  → "Close-up of an electrical cord plugged into a wall socket, a small green
     LED glowing steadily, soft ambient room light, static camera"

  VO: "you put your hands on the steering wheel"
  → "Close-up of two hands gripping a leather steering wheel, dashboard
     instruments glowing blue, passing streetlights reflected in windshield,
     slow tracking"

  VO: "that's a rope, not a snake"
  → "Close-up of a coiled length of rope on a dark stone floor, a beam of
     warm sunlight slowly crossing it revealing the braided texture, slow
     dolly in from above"

  VO: "knowledge gets covered by ignorance"
  → "Close-up of a lit candle on a wooden surface, a glass dome being slowly
     lowered over it, the flame shrinking as oxygen depletes, warm tungsten
     light, static camera"

WORDS TO NEVER USE IN PROMPTS (Nova treats them as "generic abstract"):
  metaphorical, symbolic, surreal, abstract, contemplative, spiritual,
  dreamlike, ethereal, meditative, transcendent, cosmic, infinite

NEVER USE NEGATION (Nova ignores it — "no faces" may produce faces):
  no, not, without, never, avoid

BEAT PURPOSE → FILMING STYLE (not subject replacement):
  hook:   tight framing, high contrast, dramatic side-lighting, fast dolly
  setup:  wide establishing shot, warm even lighting, static or slow pan
  build:  tracking movement, increasing shadows, handheld energy
  twist:  sudden framing change (wide→tight), bright burst of light, rack focus
  payoff: wide pullback, golden hour warmth, slow steady drift

SHOT VARIATION within a beat:
  - Shot 1 wide → shot 2 close-up → shot 3 detail (vary scale)
  - Don't repeat framing or camera motion in consecutive shots

Return the FULL screenplay JSON with only visual + broll_queries changed."""


def direct(screenplay: Screenplay) -> Screenplay:
    agent = Agent(
        model=BedrockModel(model_id=DIRECTOR_MODEL, temperature=0.4),
        system_prompt=SYSTEM,
    )
    prompt = (
        "Rewrite the visual prompts to be literal, camera-ready descriptions "
        "that match the spoken audio. Keep all other fields identical.\n\n"
        + screenplay.model_dump_json(indent=2)
    )
    return agent.structured_output(Screenplay, prompt)
