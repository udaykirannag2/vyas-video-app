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
    hook: str = Field(..., description="First 3 seconds of the reel")
    summary: str
    verse_ref: str
    target_length_sec: int = 30
    why_it_works: str
    rank: int
    # One CONTINUOUS window of the podcast this reel will use.
    window_start: float = 0.0
    window_end: float = 0.0
    window_text: str = ""
    # Narrative arc annotations — help the screenwriter assign beat types.
    hook_line: str = ""     # most attention-grabbing line in the window
    twist_line: str = ""    # key insight / reframe moment
    payoff_line: str = ""   # conclusion / takeaway
    # Deprecated
    quotes: List[Quote] = Field(default_factory=list)


class IdeasResponse(BaseModel):
    ideas: List[Idea]


class Scene(BaseModel):
    start: float
    end: float
    voiceover: str
    on_screen_text: str
    visual: str
    # Narrative beat — drives visual register and Nova Reel prompt style.
    # One of: hook | setup | build | twist | payoff
    beat_type: str = "build"
    broll_queries: List[str] = Field(default_factory=list)
    broll_query: str = ""
    source_start: float | None = None
    source_end: float | None = None


class Screenplay(BaseModel):
    title: str
    duration_sec: int
    aspect: str = "9:16"
    scenes: List[Scene]
    caption: str
    hashtags: List[str]


class ReviseRequest(BaseModel):
    screenplay: Screenplay
    instruction: str


class RenderKickoff(BaseModel):
    project_id: str
    script_s3_key: str
