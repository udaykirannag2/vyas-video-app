"""Pydantic schemas shared between agents, API, and render pipeline."""
from typing import List, Optional
from pydantic import BaseModel, Field


class Quote(BaseModel):
    """A verbatim span of the podcast that an idea plans to use."""
    start_sec: float
    end_sec: float
    text: str  # exact text from the timed transcript


class Idea(BaseModel):
    title: str
    hook: str = Field(..., description="First 3 seconds of the reel")
    summary: str
    verse_ref: str
    target_length_sec: int = 30
    why_it_works: str
    rank: int
    # One CONTINUOUS window of the podcast this reel will use. All scenes
    # will be sequential slices within this window — no jumping around.
    window_start: float = 0.0  # seconds into source audio
    window_end: float = 0.0
    window_text: str = ""  # verbatim transcript within the window
    # Deprecated: scattered quotes replaced by continuous window.
    quotes: List[Quote] = Field(default_factory=list)


class IdeasResponse(BaseModel):
    ideas: List[Idea]


class Scene(BaseModel):
    start: float
    end: float
    voiceover: str
    on_screen_text: str
    visual: str
    # Ordered list of 3 Pexels-search queries for this scene. The picker tries
    # them in order; first one with a good candidate wins. Multiple queries
    # exist because spiritual / metaphorical scenes often need several tries to
    # land a stock clip that matches the register.
    broll_queries: List[str] = Field(default_factory=list)
    # Deprecated: single-query form. Kept readable for back-compat with
    # scripts generated before the multi-query change.
    broll_query: str = ""
    # Original-audio span to slice with FFmpeg. Always populated by the
    # (strict-verbatim) screenwriter. Nullable only for back-compat with any
    # lingering pre-strict records.
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
