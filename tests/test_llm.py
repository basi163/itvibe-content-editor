import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import (
    DeepSeekClient,
    FallbackLLMClient,
    QwenClient,
    _digest_validation_errors,
)
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.renderer import SIGNATURE_TEXT, validate_post


def test_digest_validation_rejects_generic_more_link_label() -> None:
    post = GeneratedPost(
        "Технологии за день",
        "1. Apple представила устройство. [[source:0|Подробнее]]",
        ["#технологии", "#IT", "#новости"],
    )

    errors = _digest_validation_errors(post, 2000, 1)

    assert any("сам факт новости" in error for error in errors)


def test_digest_validation_rejects_source_attribution_as_link_label() -> None:
    post = GeneratedPost(
        "Технологии за день",
        "1. Apple представила устройство. [[source:0|The Verge пишет]] о релизе.",
        ["#технологии", "#IT", "#новости"],
    )

    errors = _digest_validation_errors(post, 2000, 1)

    assert any("упоминания СМИ" in error for error in errors)


def test_digest_validation_rejects_link_as_detached_second_sentence() -> None:
    post = GeneratedPost(
        "Технологии за день",
        "1. Apple представила устройство. [[source:0|Apple показала новинку]]",
        ["#технологии", "#IT", "#новости"],
    )

    errors = _digest_validation_errors(post, 2000, 1)

    assert any("после точки" in error for error in errors)


def test_digest_validation_accepts_link_inside_main_sentence() -> None:
    post = GeneratedPost(
        "Технологии за день",
        "1. [[source:0|Apple представила устройство]] с новым процессором.",
        ["#технологии", "#IT", "#новости"],
    )

    errors = _digest_validation_errors(post, 2000, 1)

    assert errors == []


def test_digest_validation_rejects_source_label_appended_at_end() -> None:
    post = GeneratedPost(
        "Технологии за день",
        (
            "1. Google анонсировала скорый выход ноутбуков-трансформеров "
            "с отсоединяемой клавиатурой [[source:0|9to5Google]]."
        ),
        ["#технологии", "#IT", "#новости"],
    )

    errors = _digest_validation_errors(post, 2000, 1)

    assert any("в первых 70 знаках" in error for error in errors)


@pytest.mark.asyncio
async def test_triage_reads_industry_critical_flag() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {
            "relevant": True,
            "score": 0.91,
            "category": "security",
            "reason": "Urgent PHP security update",
            "industry_critical": True,
            "primary_source_index": 0,
            "linkable_source_indices": [],
            "facts": ["Two critical vulnerabilities were fixed"],
            "uncertainties": [],
            "reader_interest_score": 9,
            "reader_interest_breakdown": {
                "personal_significance": 2,
                "practical_value": 2,
                "surprise": 1,
                "evidence": 2,
                "technology_story": 1,
                "business_significance": 2,
            },
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        decision = await client.triage(
            StoryCandidate("telegram_web", "https://t.me/example/1", "PHP", "Update"),
            [
                SourceDocument(
                    url="https://www.php.net/",
                    title="PHP update",
                    text="Install immediately",
                )
            ],
        )
    finally:
        await client.client.close()

    assert decision.industry_critical is True
    assert decision.relevant is True
    assert decision.reader_interest_score == 10
    assert decision.reader_interest_breakdown["practical_value"] == 2


@pytest.mark.asyncio
async def test_triage_clamps_reader_interest_breakdown() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {
            "relevant": True,
            "score": 0.8,
            "category": "technology",
            "reason": "Relevant",
            "reader_interest_breakdown": {
                "personal_significance": 5,
                "practical_value": -1,
                "surprise": "2",
                "evidence": "bad",
            },
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        decision = await client.triage(
            StoryCandidate("rss", "", "Title", "Text"),
            [SourceDocument(url="https://example.com", text="Text")],
        )
    finally:
        await client.client.close()

    assert decision.reader_interest_score == 4
    assert decision.reader_interest_breakdown == {
        "personal_significance": 2,
        "practical_value": 0,
        "surprise": 2,
        "evidence": 0,
        "technology_story": 0,
        "business_significance": 0,
    }


@pytest.mark.asyncio
async def test_repair_retries_until_post_is_valid() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )
    responses = [
        {"title": "", "body": "Текст", "hashtags": ["#ИИ", "#наука", "#тех"]},
        {"title": "Исправлено", "body": "Текст", "hashtags": ["#ИИ", "#наука", "#тех"]},
    ]

    async def completion(**_: Any) -> dict[str, Any]:
        return responses.pop(0)

    client._completion = completion  # type: ignore[method-assign]
    try:
        repaired = await client.repair(
            GeneratedPost("Плохо: да", "Текст", ["#ИИ", "#наука", "#тех"]),
            [],
            ["Двоеточие"],
        )
    finally:
        await client.client.close()

    assert repaired.title == "Исправлено"
    assert not responses


@pytest.mark.asyncio
async def test_repair_uses_local_normalizer_after_model_retries() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {
            "title": "Исправление: всё ещё с двоеточием 🚀",
            "body": "Текст: без новых фактов",
            "hashtags": ["ИИ"],
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        repaired = await client.repair(
            GeneratedPost("Плохо: да", "Текст", ["#ИИ"]),
            [],
            ["Двоеточие"],
        )
    finally:
        await client.client.close()

    assert repaired.title == "Исправление, всё ещё с двоеточием"
    assert repaired.body == "Текст, без новых фактов"
    assert repaired.hashtags == ["#ИИ", "#технологии", "#новости"]


@pytest.mark.asyncio
async def test_image_search_places_model_selection_first() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )
    tool_call = SimpleNamespace(
        type="web_search_image_call",
        output=json.dumps(
            [
                {"index": 1, "title": "First", "url": "https://images.example/1.jpg"},
                {"index": 7, "title": "Selected", "url": "https://images.example/7.jpg"},
            ]
        ),
    )
    response = SimpleNamespace(
        output=[tool_call],
        output_text='{"selected_index": 7}',
    )

    async def create(**_: Any) -> SimpleNamespace:
        return response

    client.client.responses.create = create  # type: ignore[method-assign]
    try:
        images = await client.search_images(
            StoryCandidate("solidot_rss", "", "Google account security", ""),
            [SourceDocument(url="https://example.com", title="Android security")],
            TriageDecision(True, 0.9, "security", "relevant"),
            GeneratedPost("Заголовок", "Текст", ["#ИИ", "#наука", "#тех"]),
        )
    finally:
        await client.client.close()

    assert images[0]["index"] == 7
    assert images[0]["url"] == "https://images.example/7.jpg"


@pytest.mark.asyncio
async def test_fact_check_uses_search_enabled_completion_and_returns_sources() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )
    response = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=json.dumps(
                        {
                            "facts": ["Компания опубликовала официальное заявление"],
                            "uncertainties": ["Точный масштаб пока неизвестен"],
                            "sources": [
                                {
                                    "url": "https://company.example/news/announcement",
                                    "title": "Announcement",
                                    "evidence": "В заявлении подтверждено событие",
                                }
                            ],
                        },
                        ensure_ascii=False,
                    )
                )
            )
        ]
    )
    request: dict[str, Any] = {}

    async def create(**kwargs: Any) -> SimpleNamespace:
        request.update(kwargs)
        return response

    client.client.chat.completions.create = create  # type: ignore[method-assign]
    try:
        result = await client.fact_check(
            StoryCandidate(
                "editorial_submission",
                "",
                "Важное заявление",
                "Компания сделала заявление",
                force_editorial=True,
            ),
            [SourceDocument(url="", text="Исходное сообщение")],
        )
    finally:
        await client.client.close()

    assert request["extra_body"] == {"enable_search": True, "enable_thinking": False}
    fact_check_payload = json.loads(request["messages"][1]["content"])
    assert "available_sources" not in fact_check_payload
    assert fact_check_payload["current_date"]
    assert result.facts == ["Компания опубликовала официальное заявление"]
    assert result.source_urls == ["https://company.example/news/announcement"]
    assert result.source_evidence == {
        "https://company.example/news/announcement": "В заявлении подтверждено событие"
    }


@pytest.mark.asyncio
async def test_fact_check_evidence_is_grounded_in_downloaded_pages() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {
            "facts": ["Официальное заявление подтверждает событие"],
            "uncertainties": ["Масштаб не указан"],
            "source_indices": [1],
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        result = await client.verify_fact_check(
            StoryCandidate("editorial_submission", "", "Событие", "Текст"),
            [
                SourceDocument(url="https://media.example/story", text="Пересказ"),
                SourceDocument(url="https://company.example/news", text="Заявление"),
            ],
        )
    finally:
        await client.client.close()

    assert result.facts == ["Официальное заявление подтверждает событие"]
    assert result.source_urls == ["https://company.example/news"]


@pytest.mark.asyncio
async def test_editorial_writer_only_receives_verified_material() -> None:
    async def editorial_rules_provider() -> str:
        return "Пиши короткими предложениями."

    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
        editorial_rules_provider=editorial_rules_provider,
    )
    request: dict[str, Any] = {}

    async def completion(**kwargs: Any) -> dict[str, Any]:
        request.update(kwargs)
        return {
            "title": "Подтвержденный материал",
            "body": "Компания подтвердила событие.",
            "hashtags": ["#IT", "#фактчек", "#новости"],
        }

    client._completion = completion  # type: ignore[method-assign]
    candidate = StoryCandidate(
        "editorial_submission",
        "https://t.me/example/1",
        "Непроверенный сенсационный заголовок",
        "Непроверенное утверждение из пересланного сообщения",
        force_editorial=True,
    )
    sources = [
        SourceDocument(
            url="https://t.me/example/1",
            title="Непроверенный сенсационный заголовок",
            text="Непроверенное утверждение из пересланного сообщения",
        ),
        SourceDocument(
            url="https://company.example/official",
            title="Official statement",
            text="The company confirmed the event",
            linkable=True,
        ),
    ]
    triage = TriageDecision(
        relevant=True,
        score=1.0,
        category="technology",
        reason="Ручная подача",
        linkable_source_indices=[1],
        facts=["Компания подтвердила событие"],
        uncertainties=["Масштаб события не подтвержден"],
    )
    try:
        await client.generate(candidate, sources, triage)
    finally:
        await client.client.close()

    payload = json.loads(request["user"])
    assert payload["editorial_mode"] == "verified_facts_only"
    assert payload["verified_facts"] == ["Компания подтвердила событие"]
    assert payload["uncertainties"] == ["Масштаб события не подтвержден"]
    assert payload["linkable_sources"][0]["index"] == 1
    assert "candidate" not in payload
    assert "Непроверенное утверждение" not in request["user"]
    assert "Пиши короткими предложениями." in request["system"]


@pytest.mark.asyncio
async def test_regenerate_title_changes_only_title() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {"title": "Новый взгляд на технологию"}

    client._completion = completion  # type: ignore[method-assign]
    original = GeneratedPost(
        "Старый заголовок",
        "Проверенный текст",
        ["#ИИ", "#наука", "#технологии"],
    )
    try:
        changed = await client.regenerate_component(
            "title",
            StoryCandidate("solidot_rss", "", "Original", ""),
            [SourceDocument(url="https://example.org", title="Official")],
            TriageDecision(True, 0.9, "technology", "relevant"),
            original,
        )
    finally:
        await client.client.close()

    assert changed.title == "Новый взгляд на технологию"
    assert changed.body == original.body
    assert changed.hashtags == original.hashtags


@pytest.mark.asyncio
async def test_regenerate_title_selects_a_distinct_option() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )
    original = GeneratedPost(
        "Linux меняет изоляцию",
        "Проверенный текст",
        ["#Linux", "#ядро", "#технологии"],
    )
    calls = 0

    async def completion(**_: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {
            "titles": [
                original.title,
                original.title,
                "Новый подход Linux к изоляции",
            ]
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        changed = await client.regenerate_component(
            "title",
            StoryCandidate("solidot_rss", "", "Original", ""),
            [SourceDocument(url="https://example.org", title="Official")],
            TriageDecision(True, 0.9, "technology", "relevant"),
            original,
        )
    finally:
        await client.client.close()

    assert calls == 1
    assert changed.title == "Новый подход Linux к изоляции"
    assert changed.body == original.body
    assert changed.hashtags == original.hashtags


@pytest.mark.asyncio
async def test_regenerate_title_fits_available_post_space() -> None:
    original = GeneratedPost(
        "Linux меняет изоляцию",
        "Проверенный текст без изменений.",
        ["#Linux", "#ядро", "#технологии"],
    )
    visible = f"{original.title}\n\n{original.body}\n\n{' '.join(original.hashtags)}"
    max_post_chars = len(visible) + 2 + len(SIGNATURE_TEXT)
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=max_post_chars,
    )
    payloads: list[dict[str, Any]] = []

    async def completion(**kwargs: Any) -> dict[str, Any]:
        payloads.append(json.loads(kwargs["user"]))
        return {"title": "Совершенно новый и слишком длинный заголовок о Linux"}

    client._completion = completion  # type: ignore[method-assign]
    try:
        changed = await client.regenerate_component(
            "title",
            StoryCandidate("solidot_rss", "", "Original", ""),
            [SourceDocument(url="https://example.org", title="Official")],
            TriageDecision(True, 0.9, "technology", "relevant"),
            original,
        )
    finally:
        await client.client.close()

    assert payloads[0]["maximum_title_characters"] == len(original.title)
    assert changed.title != original.title
    assert len(changed.title) <= len(original.title)
    assert changed.body == original.body
    assert changed.hashtags == original.hashtags
    assert validate_post(changed, max_post_chars) == []


@pytest.mark.asyncio
async def test_regenerate_title_prefers_an_option_that_needs_no_truncation() -> None:
    original = GeneratedPost(
        "Linux меняет изоляцию",
        "Проверенный текст без изменений.",
        ["#Linux", "#ядро", "#технологии"],
    )
    visible = f"{original.title}\n\n{original.body}\n\n{' '.join(original.hashtags)}"
    max_post_chars = len(visible) + 2 + len(SIGNATURE_TEXT)
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=max_post_chars,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {
            "titles": [
                "Очень длинный заголовок о новой системе изоляции Linux",
                "Новая изоляция Linux",
            ]
        }

    client._completion = completion  # type: ignore[method-assign]
    try:
        changed = await client.regenerate_component(
            "title",
            StoryCandidate("solidot_rss", "", "Original", ""),
            [SourceDocument(url="https://example.org", title="Official")],
            TriageDecision(True, 0.9, "technology", "relevant"),
            original,
        )
    finally:
        await client.client.close()

    assert changed.title == "Новая изоляция Linux"
    assert "…" not in changed.title
    assert validate_post(changed, max_post_chars) == []


@pytest.mark.asyncio
async def test_regenerate_hashtags_changes_only_hashtags() -> None:
    client = QwenClient(
        api_key="test",
        base_url="https://example.com/v1",
        triage_model="triage",
        writer_model="writer",
        max_post_chars=2000,
    )

    async def completion(**_: Any) -> dict[str, Any]:
        return {"hashtags": ["космос", "#Марс", "#JAXA"]}

    client._completion = completion  # type: ignore[method-assign]
    original = GeneratedPost(
        "Марсианская миссия",
        "Проверенный текст",
        ["#технологии", "#наука", "#новости"],
    )
    try:
        changed = await client.regenerate_component(
            "hashtags",
            StoryCandidate("solidot_rss", "", "Original", ""),
            [SourceDocument(url="https://example.org", title="Official")],
            TriageDecision(True, 0.9, "space", "relevant"),
            original,
        )
    finally:
        await client.client.close()

    assert changed.title == original.title
    assert changed.body == original.body
    assert changed.hashtags == ["#космос", "#Марс", "#JAXA"]


@pytest.mark.asyncio
async def test_deepseek_completion_requests_strict_json_without_qwen_options() -> None:
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        triage_model="deepseek-flash",
        writer_model="deepseek-v4-pro",
        max_post_chars=2000,
    )
    request: dict[str, Any] = {}

    async def create(**kwargs: Any) -> SimpleNamespace:
        request.update(kwargs)
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content='{"relevant": true}')
                )
            ]
        )

    client.client.chat.completions.create = create  # type: ignore[method-assign]
    try:
        result = await client._completion(
            model="deepseek-flash",
            system="Return JSON",
            user="{}",
            temperature=0.1,
            max_tokens=100,
        )
    finally:
        await client.close()

    assert result == {"relevant": True}
    assert request["response_format"] == {"type": "json_object"}
    assert request["extra_body"] == {"thinking": {"type": "disabled"}}
    assert request["max_tokens"] == 8192


@pytest.mark.asyncio
async def test_deepseek_completion_retries_empty_json_with_larger_budget() -> None:
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        triage_model="deepseek-flash",
        writer_model="deepseek-v4-pro",
        max_post_chars=2000,
    )
    requests: list[dict[str, Any]] = []

    async def create(**kwargs: Any) -> SimpleNamespace:
        requests.append(kwargs)
        content = "   " if len(requests) == 1 else '{"relevant": true}'
        return SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(content=content),
                    finish_reason="stop",
                )
            ]
        )

    client.client.chat.completions.create = create  # type: ignore[method-assign]
    try:
        result = await client._completion(
            model="deepseek-flash",
            system="Return JSON",
            user="{}",
            temperature=0.1,
            max_tokens=100,
        )
    finally:
        await client.close()

    assert result == {"relevant": True}
    assert [request["max_tokens"] for request in requests] == [8192, 16384]


@pytest.mark.asyncio
async def test_deepseek_fact_check_uses_web_search() -> None:
    client = DeepSeekClient(
        api_key="test",
        base_url="https://api.deepseek.com",
        triage_model="deepseek-flash",
        writer_model="deepseek-v4-pro",
        max_post_chars=2000,
    )
    request: dict[str, Any] = {}

    async def create(**kwargs: Any) -> SimpleNamespace:
        request.update(kwargs)
        return SimpleNamespace(
            output_text=json.dumps(
                {
                    "facts": ["Событие подтверждено"],
                    "uncertainties": [],
                    "sources": [
                        {
                            "url": "https://company.example/news",
                            "evidence": "Официальное заявление",
                        }
                    ],
                },
                ensure_ascii=False,
            )
        )

    client.client.responses.create = create  # type: ignore[method-assign]
    try:
        result = await client.fact_check(
            StoryCandidate("editorial_submission", "", "Событие", "Проверить"),
            [],
        )
    finally:
        await client.close()

    assert request["tools"] == [{"type": "web_search"}]
    assert request["text"] == {"format": {"type": "json_object"}}
    assert request["reasoning"] == {"effort": "none"}
    assert request["max_output_tokens"] == 8192
    assert result.facts == ["Событие подтверждено"]
    assert result.source_urls == ["https://company.example/news"]


@pytest.mark.asyncio
async def test_fallback_skips_primary_during_cooldown() -> None:
    primary_calls = 0
    fallback_calls = 0

    class Primary:
        async def triage(self, *_: Any) -> TriageDecision:
            nonlocal primary_calls
            primary_calls += 1
            raise RuntimeError("provider unavailable")

        async def close(self) -> None:
            return None

    class Fallback:
        async def triage(self, *_: Any) -> TriageDecision:
            nonlocal fallback_calls
            fallback_calls += 1
            return TriageDecision(True, 0.9, "technology", "fallback")

        async def close(self) -> None:
            return None

    client = FallbackLLMClient(
        Primary(),  # type: ignore[arg-type]
        Fallback(),  # type: ignore[arg-type]
        cooldown_seconds=300,
    )
    candidate = StoryCandidate("solidot_rss", "", "Новость", "Текст")
    sources = [SourceDocument(url="https://example.org", text="Текст")]

    first = await client.triage(candidate, sources)
    second = await client.triage(candidate, sources)
    await client.close()

    assert first.reason == "fallback"
    assert second.reason == "fallback"
    assert primary_calls == 1
    assert fallback_calls == 2
