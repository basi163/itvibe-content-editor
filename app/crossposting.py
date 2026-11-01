from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import httpx

from app.db import Repository
from app.models import GeneratedPost
from app.vk import VkApiError, VkGroup, VkPublisher, render_vk_crosspost

logger = logging.getLogger(__name__)

CROSSPOST_VK_ENABLED_SETTING = "crosspost_vk_enabled"
CROSSPOST_VK_GROUP_SETTING = "crosspost_vk_group"
MAX_CROSSPOST_ATTEMPTS = 4
RETRY_DELAYS = (
    timedelta(minutes=5),
    timedelta(minutes=15),
    timedelta(minutes=30),
)

FailureNotifier = Callable[[dict[str, Any], str], Awaitable[None]]


class CrosspostingService:
    def __init__(
        self,
        repository: Repository,
        vk_publisher: VkPublisher | None = None,
    ) -> None:
        self.repository = repository
        self.vk_publisher = vk_publisher
        self.wakeup = asyncio.Event()
        self._failure_notifier: FailureNotifier | None = None

    def set_failure_notifier(self, notifier: FailureNotifier) -> None:
        self._failure_notifier = notifier

    async def close(self) -> None:
        if self.vk_publisher:
            await self.vk_publisher.close()

    async def vk_target(self) -> dict[str, Any] | None:
        raw = await self.repository.get_setting(CROSSPOST_VK_GROUP_SETTING)
        if not raw:
            return None
        try:
            value = json.loads(raw)
        except (TypeError, ValueError):
            return None
        if not isinstance(value, dict) or not value.get("group_id"):
            return None
        return value

    async def set_vk_target(self, group: VkGroup) -> None:
        await self.repository.set_setting(
            CROSSPOST_VK_GROUP_SETTING,
            json.dumps(
                {
                    "group_id": group.group_id,
                    "title": group.title,
                    "screen_name": group.screen_name,
                    "url": group.url,
                },
                ensure_ascii=False,
            ),
        )

    async def vk_enabled(self) -> bool:
        return (
            await self.repository.get_setting(CROSSPOST_VK_ENABLED_SETTING)
        ) == "1"

    async def set_vk_enabled(self, enabled: bool) -> None:
        await self.repository.set_setting(
            CROSSPOST_VK_ENABLED_SETTING,
            "1" if enabled else "0",
        )
        if enabled:
            self.wakeup.set()

    async def enqueue_teletype_article(
        self,
        *,
        draft_id: int,
        article_id: int,
        article_url: str,
    ) -> int | None:
        if not await self.vk_enabled():
            return None
        target = await self.vk_target()
        if not self.vk_publisher or not target:
            return None
        group_id = int(target["group_id"])
        crosspost_id = await self.repository.enqueue_crosspost(
            draft_id=draft_id,
            source_kind="teletype",
            source_external_id=str(article_id),
            source_url=article_url,
            target_kind="vk",
            target_key=f"vk:{group_id}",
        )
        self.wakeup.set()
        return crosspost_id

    async def retry_failed(self) -> int:
        count = await self.repository.retry_failed_crossposts("vk")
        if count:
            self.wakeup.set()
        return count

    async def stats(self) -> dict[str, int]:
        return await self.repository.crosspost_stats("vk")

    async def process_due(self) -> int:
        if not self.vk_publisher or not await self.vk_enabled():
            return 0
        target = await self.vk_target()
        if not target:
            return 0
        group_id = int(target["group_id"])
        target_key = f"vk:{group_id}"
        processed = 0
        for item in await self.repository.claim_due_crossposts(target_kind="vk"):
            processed += 1
            if item.get("target_kind") != "vk" or item.get("target_key") != target_key:
                await self.repository.mark_crosspost_failed(
                    int(item["id"]),
                    "Целевое сообщество VK было изменено",
                    retry_in=None,
                )
                continue
            draft = await self.repository.get_draft(int(item["draft_id"]))
            if not draft:
                await self.repository.mark_crosspost_failed(
                    int(item["id"]),
                    "Исходный материал больше недоступен",
                    retry_in=None,
                )
                continue
            post = GeneratedPost(
                str(draft["title"]),
                str(draft["body"]),
                list(draft["hashtags"]),
            )
            try:
                post_id = await self.vk_publisher.publish_article(
                    group_id=group_id,
                    message=render_vk_crosspost(post, str(item["source_url"])),
                    article_url=str(item["source_url"]),
                    guid=(
                        f"itvibe-teletype-{item['source_external_id']}-"
                        f"vk-{group_id}"
                    ),
                    fallback_title=post.title,
                )
            except (VkApiError, httpx.HTTPError, KeyError, TypeError, ValueError) as error:
                attempts = int(item.get("attempts") or 0) + 1
                retry_in = (
                    RETRY_DELAYS[min(attempts - 1, len(RETRY_DELAYS) - 1)]
                    if attempts < MAX_CROSSPOST_ATTEMPTS
                    else None
                )
                message = str(error) or type(error).__name__
                await self.repository.mark_crosspost_failed(
                    int(item["id"]),
                    message,
                    retry_in=retry_in,
                )
                logger.warning(
                    "VK crosspost failed for Teletype article %s (attempt %s): %s",
                    item.get("source_external_id"),
                    attempts,
                    type(error).__name__,
                )
                if retry_in is None and self._failure_notifier:
                    try:
                        await self._failure_notifier(item, message)
                    except Exception:
                        logger.exception("Could not send final crosspost failure notification")
                continue
            await self.repository.mark_crosspost_published(
                int(item["id"]),
                str(post_id),
            )
            logger.info(
                "Teletype article %s crossposted to VK wall post %s",
                item.get("source_external_id"),
                post_id,
            )
        return processed

    async def worker(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self.wakeup.clear()
            try:
                await self.process_due()
            except Exception:
                logger.exception("Crossposting queue processing failed")
            try:
                await asyncio.wait_for(self.wakeup.wait(), timeout=60)
            except TimeoutError:
                pass
