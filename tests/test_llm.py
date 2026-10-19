import json
from types import SimpleNamespace
from typing import Any

import pytest

from app.llm import QwenClient
from app.models import GeneratedPost, SourceDocument, StoryCandidate, TriageDecision
from app.renderer import SIGNATURE_TEXT, validate_post


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
