"""Pydantic schemas shared between agents, API, and render pipeline."""
from typing import List, Optional
from pydantic import BaseModel, Field


class Quote(BaseModel):
    """Deprecated — replaced by continuous window. Kept for old data."""
    start_sec: float
    end_sec: float
    text: str


class Idea(BaseModel):
    title: str
    # Alternate titles the user can pick from at publish time.
    alt_title_1: str = ""  # stronger question version
    alt_title_2: str = ""  # stronger contrast version
    # First-line overlay text for the reel cover thumbnail.
    hook_title: str = ""
    # Reader-facing blurb (tension → insight hint → soft CTA).
    # Used for YouTube/Instagram caption. Different from `summary` which is
    # an internal narrative arc description.
    description: str = ""
    hook: str = Field(..., description="First 3 seconds of the reel")
    summary: str
    verse_ref: str
    target_length_sec: int = 30
    why_it_works: str
    rank: int
    window_start: float = 0.0
    window_end: float = 0.0
    window_text: str = ""
    hook_line: str = ""
    twist_line: str = ""
    payoff_line: str = ""
    quotes: List[Quote] = Field(default_factory=list)


class IdeasResponse(BaseModel):
    ideas: List[Idea]


# ---- Multi-shot beat structure ----

class Shot(BaseModel):
    """One visual clip within a beat. A beat typically has 2-4 shots."""
    shot_number: int
    shot_duration_sec: float  # how long this shot holds on screen
    # Role in the visual storytelling:
    #   hook / establish / detail / contrast / payoff / reflection
    shot_role: str = "establish"
    # Visual abstraction level:
    #   literal (concrete, recognizable) / hybrid / metaphorical (abstract, symbolic)
    visual_mode: str = "metaphorical"
    visual: str  # cinematic scene description for Nova Reel
    framing: str = ""  # close-up / medium / wide / extreme-close-up / aerial
    camera_movement: str = ""  # slow zoom / static / tracking / pull back / dolly
    transition_hint: str = ""  # cut / dissolve / match-cut / fade
    broll_queries: List[str] = Field(default_factory=list)
    broll_query: str = ""


class Beat(BaseModel):
    """One spoken segment of the reel. Contains the audio voiceover and
    2-4 visual shots that play during that voiceover."""
    # Reel timeline (set by _align_beat_timelines, not the agent).
    start: float = 0.0
    end: float = 0.0
    # Source podcast audio span.
    source_start: float | None = None
    source_end: float | None = None
    voiceover: str
    on_screen_text: str
    # Narrative purpose: hook | setup | build | twist | payoff
    purpose: str = "build"
    # 2-4 visual shots that tile across this beat's duration.
    shots: List[Shot] = Field(default_factory=list)


class Screenplay(BaseModel):
    title: str
    duration_sec: int
    aspect: str = "9:16"
    beats: List[Beat]
    caption: str
    hashtags: List[str]
    # Deprecated: old single-shot scenes. Kept so GET /script doesn't crash
    # on data written before the multi-shot migration.
    scenes: List[dict] = Field(default_factory=list)


class ReviseRequest(BaseModel):
    screenplay: Screenplay
    instruction: str


class RenderKickoff(BaseModel):
    project_id: str
    script_s3_key: str
