import json
from types import SimpleNamespace

import pytest

from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
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


@pytest.mark.asyncio
async def test_story_without_image_is_not_created() -> None:
    class Extractor:
        async def collect_sources(self, urls: list[str]) -> list[SourceDocument]:
            return [
                SourceDocument(
                    url="https://example.org/official",
                    title="Official",
                    text="Verified source",
                )
            ]

        async def download_image(self, url: str) -> None:
            return None

    class Llm:
        async def triage(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> TriageDecision:
            return TriageDecision(True, 0.9, "technology", "relevant", primary_source_index=0)

        async def generate(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
        ) -> GeneratedPost:
            return GeneratedPost(
                "Заголовок",
                "Проверенный текст",
                ["#ИИ", "#наука", "#технологии"],
            )

        async def search_images(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
            post: GeneratedPost,
        ) -> list[dict[str, object]]:
            return []

    class Repository:
        async def create_draft(self, **kwargs: object) -> int:
            raise AssertionError("A draft without an image must not be created")

    pipeline = ContentPipeline(
        settings=SimpleNamespace(relevance_threshold=0.72, max_post_chars=2000),
        repository=Repository(),
        extractor=Extractor(),
        llm=Llm(),
    )
    candidate = StoryCandidate(
        origin_type="manual",
        origin_ref="https://example.org/story",
        title="Story",
        text="Text",
        urls=["https://example.org/official"],
    )

    assert await pipeline._process(candidate) is None


@pytest.mark.asyncio
async def test_story_uses_contextual_image_when_sources_have_none() -> None:
    image_url = "https://images.example/context.jpg"

    class Extractor:
        async def collect_sources(self, urls: list[str]) -> list[SourceDocument]:
            return [SourceDocument(url="https://example.org/story", title="Official")]

        async def download_image(self, url: str) -> tuple[bytes, str] | None:
            assert url == image_url
            return b"x" * 20_000, "preview.jpg"

    class Llm:
        async def triage(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> TriageDecision:
            return TriageDecision(True, 0.9, "technology", "relevant")

        async def generate(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
        ) -> GeneratedPost:
            return GeneratedPost(
                "Заголовок",
                "Проверенный текст",
                ["#ИИ", "#наука", "#тех"],
            )

        async def search_images(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
            post: GeneratedPost,
        ) -> list[dict[str, object]]:
            return [{"index": 2, "title": "Context", "url": image_url}]

    class Repository:
        created: dict[str, object] | None = None

        async def create_draft(self, **kwargs: object) -> int:
            self.created = kwargs
            return 42

    repository = Repository()
    pipeline = ContentPipeline(
        settings=SimpleNamespace(relevance_threshold=0.72, max_post_chars=2000),
        repository=repository,
        extractor=Extractor(),
        llm=Llm(),
    )
    candidate = StoryCandidate("manual", "https://example.org/story", "Story", "Text")

    assert await pipeline._process(candidate) == 42
    assert repository.created is not None
    media = repository.created["media"]
    assert isinstance(media, list)
    assert media[0]["provider"] == "qwen_web_search_image"


@pytest.mark.asyncio
async def test_regenerate_media_finds_a_different_contextual_image() -> None:
    old_url = "https://images.example/old.jpg"
    second_old_url = "https://images.example/second-old.jpg"
    new_url = "https://images.example/new.jpg"
    draft = {
        "id": 9,
        "title": "Заголовок",
        "body": "Проверенный текст",
        "hashtags": ["#ИИ", "#наука", "#технологии"],
        "media": [
            {"type": "photo", "image_url": old_url},
            {"type": "photo", "image_url": second_old_url},
        ],
        "preview_index": 0,
        "material": {
            "candidate": StoryCandidate("solidot_rss", "", "Story", "").to_dict(),
            "sources": [SourceDocument(url="https://example.org").to_dict()],
        },
        "triage": TriageDecision(True, 0.9, "technology", "relevant").to_dict(),
    }

    class Repository:
        updated: dict[str, object] | None = None

        async def get_draft(self, draft_id: int) -> dict[str, object] | None:
            assert draft_id == 9
            return draft

        async def update_draft(self, draft_id: int, **fields: object) -> None:
            assert draft_id == 9
            self.updated = fields

    class Extractor:
        async def download_image(self, url: str) -> tuple[bytes, str] | None:
            assert url == new_url
            return b"x" * 20_000, "new.jpg"

    class Llm:
        async def search_images(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
            post: GeneratedPost,
            excluded_urls: list[str] | None = None,
        ) -> list[dict[str, object]]:
            assert excluded_urls == [old_url, second_old_url]
            return [{"index": 1, "title": "Alternative", "url": new_url}]

    repository = Repository()
    pipeline = ContentPipeline(
        settings=SimpleNamespace(max_post_chars=2000),
        repository=repository,
        extractor=Extractor(),
        llm=Llm(),
    )

    assert await pipeline.regenerate_media(9)
    assert repository.updated is not None
    media = json.loads(str(repository.updated["media_json"]))
    assert media[0]["image_url"] == new_url
    assert media[1]["image_url"] == old_url
    assert media[2]["image_url"] == second_old_url
    assert repository.updated["preview_index"] == 0
