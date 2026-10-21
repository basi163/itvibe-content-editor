from __future__ import annotations

import asyncio
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from app.db import Repository
from app.models import StoryCandidate
from app.pipeline import ContentPipeline
from app.rss import FeedEntry, RssFeed, normalize_rss_url

logger = logging.getLogger(__name__)
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS = 3
URL_IN_ERROR_RE = re.compile(r"https?://\S+", re.IGNORECASE)


def _safe_error(error: Exception) -> str:
    message = " ".join(str(error).split())
    message = URL_IN_ERROR_RE.sub("[URL]", message)
    return f"{type(error).__name__}: {message}"[:500]


class RssSourceManager:
    kind = "rss"
    builtin_source_key = "solidot"

    def __init__(
        self,
        repository: Repository,
        pipeline: ContentPipeline,
        *,
        timeout: float,
        builtin_url: str,
        bootstrap_items: int,
    ) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self.timeout = timeout
        self.builtin_url = normalize_rss_url(builtin_url)
        self.bootstrap_items = bootstrap_items
        self._locks: dict[int, asyncio.Lock] = {}

    @staticmethod
    def source_key(url: str) -> str:
        digest = hashlib.sha256(normalize_rss_url(url).encode()).hexdigest()[:24]
        return f"rss:{digest}"

    async def ensure_builtin(self) -> dict[str, Any]:
        existing = await self.repository.get_content_source_by_key(
            self.builtin_source_key
        )
        if existing:
            if existing["kind"] != self.kind or existing["url"] != self.builtin_url:
                await self.repository.update_content_source(
                    int(existing["id"]),
                    url=self.builtin_url,
                    title="Solidot RSS",
                )
                existing = await self.repository.get_content_source(int(existing["id"]))
            if not existing:
                raise RuntimeError("Не удалось загрузить Solidot RSS")
            return existing
        source_id = await self.repository.create_content_source(
            kind=self.kind,
            source_key=self.builtin_source_key,
            title="Solidot RSS",
            url=self.builtin_url,
            last_external_id=None,
        )
        source = await self.repository.get_content_source(source_id)
        if not source:
            raise RuntimeError("Не удалось сохранить Solidot RSS")
        return source

    async def add(self, value: str) -> tuple[dict[str, Any], bool]:
        url = normalize_rss_url(value)
        for source in await self.repository.content_sources(kind=self.kind):
            if normalize_rss_url(str(source["url"])) == url:
                return source, False

        snapshot = await RssFeed(url, self.timeout).fetch()
        source_key = self.source_key(snapshot.url)
        existing = await self.repository.get_content_source_by_key(source_key)
        if existing:
            return existing, False
        latest_id = snapshot.entries[0].external_id if snapshot.entries else None
        source_id = await self.repository.create_content_source(
            kind=self.kind,
            source_key=source_key,
            title=snapshot.title,
            url=snapshot.url,
            last_external_id=latest_id,
        )
        for entry in snapshot.entries:
            await self.repository.add_feed_item(
                source_key,
                entry.external_id,
                entry.link,
                entry.title,
                entry.published_at,
                "baseline",
                entry.raw,
            )
        source = await self.repository.get_content_source(source_id)
        if not source:
            raise RuntimeError("Не удалось сохранить RSS-источник")
        return source, True

    async def poll_source(self, source: dict[str, Any]) -> dict[str, int]:
        source_id = int(source["id"])
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        if lock.locked():
            return {"found": 0, "processed": 0, "drafts": 0}
        async with lock:
            checked_at = datetime.now(UTC).isoformat()
            try:
                snapshot = await RssFeed(str(source["url"]), self.timeout).fetch()
                source_key = str(source["source_key"])
                unseen: list[FeedEntry] = []
                retries: list[tuple[int, FeedEntry]] = []
                for entry in snapshot.entries:
                    state = await self.repository.get_feed_item_state(
                        source_key,
                        entry.external_id,
                    )
                    if state is None:
                        unseen.append(entry)
                    elif self._retryable(state):
                        retries.append((int(state["id"]), entry))

                if (
                    source_key == self.builtin_source_key
                    and not await self.repository.feed_has_items(source_key)
                    and len(unseen) > self.bootstrap_items
                ):
                    for entry in unseen[self.bootstrap_items :]:
                        await self.repository.add_feed_item(
                            source_key,
                            entry.external_id,
                            entry.link,
                            entry.title,
                            entry.published_at,
                            "baseline",
                            entry.raw,
                        )
                    unseen = unseen[: self.bootstrap_items]

                processed = 0
                drafts = 0
                for entry in reversed(unseen):
                    draft_id = await self._store_and_process(source, entry)
                    processed += 1
                    drafts += int(draft_id is not None)
                for item_id, entry in reversed(retries):
                    await self.repository.mark_feed_processing(item_id)
                    draft_id = await self._process(source, item_id, entry)
                    processed += 1
                    drafts += int(draft_id is not None)

                latest_id = (
                    snapshot.entries[0].external_id if snapshot.entries else None
                )
                await self.repository.update_content_source(
                    source_id,
                    title=(
                        "Solidot RSS"
                        if source_key == self.builtin_source_key
                        else snapshot.title
                    ),
                    url=snapshot.url,
                    last_external_id=latest_id,
                    last_checked_at=checked_at,
                    last_success_at=checked_at,
                    last_error=None,
                )
                return {
                    "found": len(unseen),
                    "processed": processed,
                    "drafts": drafts,
                }
            except Exception as error:
                logger.warning(
                    "RSS source %s check failed: %s",
                    source_id,
                    type(error).__name__,
                )
                await self.repository.update_content_source(
                    source_id,
                    last_checked_at=checked_at,
                    last_error=_safe_error(error),
                )
                raise

    async def _store_and_process(
        self,
        source: dict[str, Any],
        entry: FeedEntry,
    ) -> int | None:
        item_id = await self.repository.add_feed_item(
            str(source["source_key"]),
            entry.external_id,
            entry.link,
            entry.title,
            entry.published_at,
            "processing",
            entry.raw,
        )
        return await self._process(source, item_id, entry)

    async def _process(
        self,
        source: dict[str, Any],
        item_id: int,
        entry: FeedEntry,
    ) -> int | None:
        source_key = str(source["source_key"])
        candidate = StoryCandidate(
            origin_type=(
                "solidot_rss" if source_key == self.builtin_source_key else "rss"
            ),
            origin_ref=entry.link,
            title=entry.title,
            text=entry.summary,
            urls=entry.urls,
            media_urls=list(entry.raw.get("media_urls", [])),
        )
        return await self.pipeline.process_feed_item(item_id, candidate)

    @staticmethod
    def _retryable(state: dict[str, Any]) -> bool:
        if state.get("status") != "failed":
            return False
        if int(state.get("attempt_count") or 0) >= MAX_ATTEMPTS:
            return False
        try:
            updated_at = datetime.fromisoformat(str(state["updated_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - updated_at >= RETRY_DELAY

    async def poll_all(self) -> dict[str, int]:
        totals = {
            "sources": 0,
            "found": 0,
            "processed": 0,
            "drafts": 0,
            "errors": 0,
        }
        sources = await self.repository.content_sources(
            kind=self.kind,
            enabled_only=True,
        )
        totals["sources"] = len(sources)
        for source in sources:
            try:
                result = await self.poll_source(source)
            except Exception:  # noqa: BLE001 - one feed must not stop the others
                totals["errors"] += 1
                continue
            for key in ("found", "processed", "drafts"):
                totals[key] += result[key]
        return totals
