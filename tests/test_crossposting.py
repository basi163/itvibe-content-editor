from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.crossposting import (
    CROSSPOST_VK_ENABLED_SETTING,
    CROSSPOST_VK_GROUP_SETTING,
    CrosspostingService,
)


def _setting_reader(enabled: bool = True):
    async def get_setting(key: str) -> str | None:
        if key == CROSSPOST_VK_ENABLED_SETTING:
            return "1" if enabled else "0"
        if key == CROSSPOST_VK_GROUP_SETTING:
            return json.dumps(
                {
                    "group_id": 42,
                    "title": "IT Vibe",
                    "screen_name": "it_vibe",
                    "url": "https://vk.ru/it_vibe",
                }
            )
        return None

    return get_setting


@pytest.mark.asyncio
async def test_teletype_publication_enqueues_enabled_vk_crosspost() -> None:
    repository = SimpleNamespace(
        get_setting=AsyncMock(side_effect=_setting_reader()),
        enqueue_crosspost=AsyncMock(return_value=7),
    )
    publisher = SimpleNamespace()
    service = CrosspostingService(repository, publisher)

    result = await service.enqueue_teletype_article(
        draft_id=12,
        article_id=99,
        article_url="https://teletype.in/@itvibe/launch",
    )

    assert result == 7
    repository.enqueue_crosspost.assert_awaited_once_with(
        draft_id=12,
        source_kind="teletype",
        source_external_id="99",
        source_url="https://teletype.in/@itvibe/launch",
        target_kind="vk",
        target_key="vk:42",
    )
    assert service.wakeup.is_set()


@pytest.mark.asyncio
async def test_disabled_vk_does_not_create_queue_item() -> None:
    repository = SimpleNamespace(
        get_setting=AsyncMock(side_effect=_setting_reader(enabled=False)),
        enqueue_crosspost=AsyncMock(),
    )
    service = CrosspostingService(repository, SimpleNamespace())

    result = await service.enqueue_teletype_article(
        draft_id=12,
        article_id=99,
        article_url="https://teletype.in/@itvibe/launch",
    )

    assert result is None
    repository.enqueue_crosspost.assert_not_awaited()


@pytest.mark.asyncio
async def test_worker_publishes_due_vk_crosspost_and_marks_it_complete() -> None:
    repository = SimpleNamespace(
        get_setting=AsyncMock(side_effect=_setting_reader()),
        claim_due_crossposts=AsyncMock(
            return_value=[
                {
                    "id": 5,
                    "draft_id": 12,
                    "source_external_id": "99",
                    "source_url": "https://teletype.in/@itvibe/launch",
                    "target_kind": "vk",
                    "target_key": "vk:42",
                    "attempts": 0,
                }
            ]
        ),
        get_draft=AsyncMock(
            return_value={
                "title": "Новый релиз",
                "body": "Полный текст.",
                "hashtags": ["#IT", "#новости", "#технологии"],
            }
        ),
        mark_crosspost_published=AsyncMock(),
        mark_crosspost_failed=AsyncMock(),
    )
    publisher = SimpleNamespace(
        publish_article=AsyncMock(return_value=101),
    )
    service = CrosspostingService(repository, publisher)

    processed = await service.process_due()

    assert processed == 1
    repository.claim_due_crossposts.assert_awaited_once_with(target_kind="vk")
    publisher.publish_article.assert_awaited_once()
    call = publisher.publish_article.await_args.kwargs
    assert call["group_id"] == 42
    assert call["article_url"] == "https://teletype.in/@itvibe/launch"
    assert call["guid"] == "itvibe-teletype-99-vk-42"
    assert "Новый релиз" in call["message"]
    repository.mark_crosspost_published.assert_awaited_once_with(5, "101")
    repository.mark_crosspost_failed.assert_not_awaited()
