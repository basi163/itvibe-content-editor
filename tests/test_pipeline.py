import json
from types import SimpleNamespace

import pytest

from app.models import (
    FactCheckResult,
    GeneratedPost,
    SourceDocument,
    StoryCandidate,
    TriageDecision,
)
from app.pipeline import ContentPipeline, is_digest_candidate


def test_telegram_digest_detection_requires_multiple_links_and_digest_shape() -> None:
    candidate = StoryCandidate(
        "telegram_web",
        "https://t.me/example/42",
        "#News",
        "自留地晚报\n1️⃣ First\n2️⃣ Second\n3️⃣ Third",
        urls=[
            "https://example.com/1",
            "https://example.com/2",
            "https://example.com/3",
        ],
    )

    assert is_digest_candidate(candidate)
    candidate.urls = candidate.urls[:2]
    assert not is_digest_candidate(candidate)


@pytest.mark.asyncio
async def test_telegram_digest_uses_direct_links_and_skips_regular_triage() -> None:
    urls = [
        "https://media.example/one",
        "https://company.example/two",
        "https://social.example/three",
    ]

    class Extractor:
        async def collect_sources(self, source_urls: list[str]) -> list[SourceDocument]:
            raise AssertionError("A digest must not use recursive source discovery")

        async def collect_direct_sources(
            self,
            source_urls: list[str],
            *,
            limit: int,
        ) -> list[SourceDocument]:
            assert source_urls == urls
            assert limit == 10
            return [SourceDocument(url=url, title=f"Source {index}") for index, url in enumerate(urls)]

    class Llm:
        async def triage(self, *_: object) -> TriageDecision:
            raise AssertionError("A recognized digest must bypass article triage")

        async def generate_digest(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
        ) -> GeneratedPost:
            assert triage.category == "digest"
            assert [source.url for source in sources] == urls
            assert all(source.linkable for source in sources)
            return GeneratedPost(
                "Технологический дайджест",
                (
                    "1. [[source:0|Первая новость]] коротко.\n\n"
                    "2. [[source:1|Вторая новость]] коротко.\n\n"
                    "3. [[source:2|Третья новость]] коротко."
                ),
                ["#технологии", "#дайджест", "#IT"],
            )

    class Repository:
        created: dict[str, object] | None = None

        async def create_draft(self, **kwargs: object) -> int:
            self.created = kwargs
            return 55

    repository = Repository()
    pipeline = ContentPipeline(
        settings=SimpleNamespace(relevance_threshold=0.72, max_post_chars=2000),
        repository=repository,
        extractor=Extractor(),
        llm=Llm(),
    )
    candidate = StoryCandidate(
        "telegram_web",
        "https://t.me/example/42",
        "#News",
        "晚报\n1️⃣ First\n2️⃣ Second\n3️⃣ Third",
        urls=urls,
        media_urls=["https://images.example/digest.jpg"],
    )

    assert await pipeline._process(candidate) == 55
    assert repository.created is not None
    assert repository.created["triage"]["category"] == "digest"  # type: ignore[index]
    assert all(source["linkable"] for source in repository.created["sources"])  # type: ignore[union-attr]


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


def test_telegram_public_image_is_available_to_the_existing_media_pipeline() -> None:
    image_url = "https://cdn.example.org/channel-image.jpg"
    candidate = StoryCandidate(
        "telegram_web",
        "https://t.me/example_channel/42",
        "Title",
        "Text",
        media_urls=[image_url],
    )

    media = ContentPipeline._media_candidates(
        candidate,
        [],
        TriageDecision(True, 0.9, "technology", "relevant"),
    )

    assert media[0]["image_url"] == image_url
    assert media[0]["provider"] == "telegram_public_web"


@pytest.mark.asyncio
async def test_industry_critical_story_bypasses_threshold_and_is_marked() -> None:
    class Extractor:
        async def collect_sources(self, urls: list[str]) -> list[SourceDocument]:
            return []

    class Llm:
        async def triage(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> TriageDecision:
            return TriageDecision(
                relevant=False,
                score=0.2,
                category="security",
                reason="Critical update for widely used infrastructure",
                industry_critical=True,
            )

        async def generate(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
        ) -> GeneratedPost:
            assert triage.relevant is True
            assert triage.score == 0.72
            return GeneratedPost(
                "PHP закрывает критические уязвимости",
                "Разработчики выпустили срочное обновление.",
                ["#PHP", "#безопасность", "#обновление"],
            )

    class Repository:
        created: dict[str, object] | None = None

        async def create_draft(self, **kwargs: object) -> int:
            self.created = kwargs
            return 45

    repository = Repository()
    pipeline = ContentPipeline(
        settings=SimpleNamespace(relevance_threshold=0.72, max_post_chars=2000),
        repository=repository,
        extractor=Extractor(),
        llm=Llm(),
    )
    candidate = StoryCandidate(
        "telegram_web",
        "https://t.me/example/1",
        "Critical PHP update",
        "Install the security update",
        media_urls=["https://images.example/php.jpg"],
    )

    assert await pipeline._process(candidate) == 45
    assert repository.created is not None
    assert repository.created["title"] == "⚡️ PHP закрывает критические уязвимости"
    triage = repository.created["triage"]
    assert isinstance(triage, dict)
    assert triage["industry_critical"] is True


@pytest.mark.asyncio
async def test_editorial_submission_is_fact_checked_and_bypasses_relevance_filter() -> None:
    official_url = "https://company.example/official-announcement"

    class Extractor:
        async def collect_sources(self, urls: list[str]) -> list[SourceDocument]:
            return []

        async def collect_direct_sources(
            self,
            urls: list[str],
            *,
            limit: int,
        ) -> list[SourceDocument]:
            assert urls == [official_url]
            assert limit == 6
            return [
                SourceDocument(
                    url=official_url,
                    title="Official announcement",
                    text="",
                    publisher="Company",
                )
            ]

    class Llm:
        async def fact_check(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> FactCheckResult:
            assert candidate.force_editorial is True
            assert sources[0].text == candidate.text
            return FactCheckResult(
                facts=["Компания подтвердила событие"],
                uncertainties=["Масштаб не указан"],
                source_urls=[official_url],
                source_evidence={official_url: "The company confirmed the event"},
            )

        async def verify_fact_check(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> FactCheckResult:
            assert candidate.force_editorial is True
            assert [source.url for source in sources] == [official_url]
            assert sources[0].text == (
                "Фрагмент веб-поиска: The company confirmed the event"
            )
            return FactCheckResult(
                facts=["Компания подтвердила событие"],
                uncertainties=["Масштаб не указан"],
                source_urls=[official_url],
            )

        async def triage(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
        ) -> TriageDecision:
            assert sources[-1].url == official_url
            return TriageDecision(
                relevant=False,
                score=0.1,
                category="other",
                reason="Вне обычной тематики",
                linkable_source_indices=[1],
                facts=["Непроверенная формулировка"],
            )

        async def generate(
            self,
            candidate: StoryCandidate,
            sources: list[SourceDocument],
            triage: TriageDecision,
        ) -> GeneratedPost:
            assert triage.relevant is True
            assert triage.score == 0.65
            assert triage.facts == ["Компания подтвердила событие"]
            assert triage.uncertainties == ["Масштаб не указан"]
            return GeneratedPost(
                "Проверенный материал",
                "[[source:1|Компания подтвердила]] событие.",
                ["#новости", "#фактчек", "#IT"],
            )

    class Repository:
        created: dict[str, object] | None = None

        async def create_draft(self, **kwargs: object) -> int:
            self.created = kwargs
            return 77

    repository = Repository()
    pipeline = ContentPipeline(
        settings=SimpleNamespace(
            relevance_threshold=0.65,
            max_post_chars=2000,
            max_source_links=6,
        ),
        repository=repository,
        extractor=Extractor(),
        llm=Llm(),
    )
    candidate = StoryCandidate(
        origin_type="editorial_submission",
        origin_ref="",
        title="Ручной материал",
        text="Здесь может быть текст на любую тему",
        media_urls=["https://images.example/manual.jpg"],
        force_editorial=True,
    )

    assert await pipeline._process(candidate) == 77
    assert repository.created is not None
    assert repository.created["origin_type"] == "editorial_submission"
    assert repository.created["sources"][1]["linkable"] is True  # type: ignore[index]
    assert repository.created["material"]["fact_check"]["source_urls"] == [official_url]  # type: ignore[index]


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
