from app.models import SourceDocument, StoryCandidate, TriageDecision
from app.pipeline import ContentPipeline


def test_article_image_is_preferred_over_official_site_card() -> None:
    candidate = StoryCandidate("solidot_rss", "", "Title", "Text")
    sources = [
        SourceDocument(
            url="https://www.zdnet.com/business/story/",
            og_image="https://www.zdnet.com/wp-content/hero.jpg",
        ),
        SourceDocument(
            url="https://government.example/document/1",
            og_image="https://government.example/logo.png",
            linkable=True,
        ),
    ]
    triage = TriageDecision(True, 0.9, "policy", "relevant", primary_source_index=1)

    media = ContentPipeline._media_candidates(candidate, sources, triage)

    assert media[0]["image_url"] == "https://www.zdnet.com/wp-content/hero.jpg"
