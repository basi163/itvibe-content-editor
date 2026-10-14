from typing import Any

import pytest

from app.llm import QwenClient
from app.models import GeneratedPost


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
        {"title": "Всё ещё: плохо", "body": "Текст", "hashtags": ["#ИИ", "#наука", "#тех"]},
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
