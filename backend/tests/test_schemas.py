from models import Idea, Screenplay, Scene


def test_idea_roundtrip():
    i = Idea(
        title="Stop Chasing Outcomes",
        hook="What if losing meant winning?",
        summary="Reframe of BG 2.47 for exam-anxious students.",
        verse_ref="BG 2.47",
        target_length_sec=30,
        why_it_works="Taps into performance anxiety common in 15-35",
        rank=1,
    )
    assert i.model_dump()["rank"] == 1


def test_screenplay_roundtrip():
    s = Screenplay(
        title="Stop Chasing Outcomes",
        duration_sec=30,
        aspect="9:16",
        scenes=[
            Scene(
                start=0, end=3,
                voiceover="What if losing meant winning?",
                on_screen_text="WHAT IF?",
                visual="close up eyes opening",
                broll_query="eyes opening slow motion",
            )
        ],
        caption="Detach from outcome. Focus on action.",
        hashtags=["#BhagavadGita", "#Wisdom"],
    )
    assert s.scenes[0].end == 3
