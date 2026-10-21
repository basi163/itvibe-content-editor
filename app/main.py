from __future__ import annotations

import asyncio
import logging
import signal
from datetime import UTC, datetime, timedelta, timezone

from app.bot import EditorBot
from app.config import Settings
from app.db import Repository
from app.extractor import ArticleExtractor
from app.llm import QwenClient
from app.models import StoryCandidate
from app.pipeline import ContentPipeline
from app.rss import FeedEntry, SolidotFeed
from app.telegram_sources import TelegramSourceManager

logger = logging.getLogger(__name__)
FEED_RETRY_DELAY = timedelta(minutes=30)
MAX_FEED_ATTEMPTS = 3


def rss_poll_delay_seconds(settings: Settings, now: datetime | None = None) -> int:
    """Return the active/off-peak delay, without sleeping past an active-window start."""
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    local = current.astimezone(
        timezone(timedelta(hours=settings.rss_schedule_utc_offset_hours))
    )
    minute_of_day = local.hour * 60 + local.minute

    for start, end in settings.rss_active_windows:
        is_active = (
            start <= minute_of_day < end
            if start < end
            else minute_of_day >= start or minute_of_day < end
        )
        if is_active:
            return settings.rss_poll_interval_seconds

    seconds_of_day = minute_of_day * 60 + local.second
    seconds_until_start = min(
        ((start * 60 - seconds_of_day) % (24 * 60 * 60)) or 24 * 60 * 60
        for start, _ in settings.rss_active_windows
    )
    return max(
        1,
        min(settings.rss_offpeak_poll_interval_seconds, seconds_until_start),
    )


async def _store_and_process(
    entry: FeedEntry,
    repository: Repository,
    pipeline: ContentPipeline,
) -> None:
    item_id = await repository.add_feed_item(
        SolidotFeed.source_key,
        entry.external_id,
        entry.link,
        entry.title,
        entry.published_at,
        "processing",
        entry.raw,
    )
    candidate = StoryCandidate(
        origin_type="solidot_rss",
        origin_ref=entry.link,
        title=entry.title,
        text=entry.summary,
        urls=entry.urls,
    )
    await pipeline.process_feed_item(item_id, candidate)


async def _retry_and_process(
    item_id: int,
    entry: FeedEntry,
    repository: Repository,
    pipeline: ContentPipeline,
) -> None:
    await repository.mark_feed_processing(item_id)
    candidate = StoryCandidate(
        origin_type="solidot_rss",
        origin_ref=entry.link,
        title=entry.title,
        text=entry.summary,
        urls=entry.urls,
    )
    await pipeline.process_feed_item(item_id, candidate)


def _retryable_feed_item(state: dict[str, object], now: datetime | None = None) -> bool:
    if state.get("status") != "failed":
        return False
    if not str(state.get("error") or "").startswith("ValueError"):
        return False
    if int(state.get("attempt_count") or 0) >= MAX_FEED_ATTEMPTS:
        return False
    try:
        updated_at = datetime.fromisoformat(str(state["updated_at"]))
    except (KeyError, TypeError, ValueError):
        return False
    if updated_at.tzinfo is None:
        updated_at = updated_at.replace(tzinfo=UTC)
    return (now or datetime.now(UTC)) - updated_at >= FEED_RETRY_DELAY


async def rss_worker(
    settings: Settings,
    repository: Repository,
    pipeline: ContentPipeline,
    stop_event: asyncio.Event,
) -> None:
    feed = SolidotFeed(settings.solidot_rss_url, settings.http_timeout_seconds)
    previous_delay: int | None = None
    while not stop_event.is_set():
        try:
            entries = await feed.fetch()
            has_history = await repository.feed_has_items(SolidotFeed.source_key)
            unseen: list[FeedEntry] = []
            retries: list[tuple[int, FeedEntry]] = []
            for entry in entries:
                state = await repository.get_feed_item_state(
                    SolidotFeed.source_key,
                    entry.external_id,
                )
                if state is None:
                    unseen.append(entry)
                elif _retryable_feed_item(state):
                    retries.append((int(state["id"]), entry))
            if not has_history and len(unseen) > settings.rss_bootstrap_items:
                baseline = unseen[settings.rss_bootstrap_items :]
                for entry in baseline:
                    await repository.add_feed_item(
                        SolidotFeed.source_key,
                        entry.external_id,
                        entry.link,
                        entry.title,
                        entry.published_at,
                        "baseline",
                        entry.raw,
                    )
                unseen = unseen[: settings.rss_bootstrap_items]
            for entry in reversed(unseen):
                await _store_and_process(entry, repository, pipeline)
            for item_id, entry in reversed(retries):
                logger.info("Retrying failed feed item %s", item_id)
                await _retry_and_process(item_id, entry, repository, pipeline)
        except Exception:
            logger.exception("Solidot RSS polling failed")

        delay = rss_poll_delay_seconds(settings)
        if delay != previous_delay:
            logger.info("Next Solidot RSS check in %s seconds", delay)
            previous_delay = delay
        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=delay,
            )
        except TimeoutError:
            pass


async def telegram_source_worker(
    settings: Settings,
    manager: TelegramSourceManager,
    stop_event: asyncio.Event,
) -> None:
    previous_delay: int | None = None
    while not stop_event.is_set():
        try:
            result = await manager.poll_all()
            if result["found"] or result["errors"]:
                logger.info(
                    "Telegram sources checked: sources=%s found=%s processed=%s "
                    "drafts=%s errors=%s",
                    result["sources"],
                    result["found"],
                    result["processed"],
                    result["drafts"],
                    result["errors"],
                )
        except Exception:
            logger.exception("Telegram source polling failed")

        delay = rss_poll_delay_seconds(settings)
        if delay != previous_delay:
            logger.info("Next Telegram source check in %s seconds", delay)
            previous_delay = delay
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=delay)
        except TimeoutError:
            pass


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    settings = Settings.load()
    repository = Repository(settings.database_path)
    await repository.initialize()
    extractor = ArticleExtractor(settings.http_timeout_seconds, settings.max_source_links)
    llm = QwenClient(
        api_key=settings.qwen_api_key,
        base_url=settings.llm_base_url,
        triage_model=settings.triage_model,
        writer_model=settings.writer_model,
        max_post_chars=settings.max_post_chars,
    )
    pipeline = ContentPipeline(
        settings=settings,
        repository=repository,
        extractor=extractor,
        llm=llm,
    )
    telegram_sources = TelegramSourceManager(
        repository,
        pipeline,
        timeout=settings.http_timeout_seconds,
    )
    editor_bot = EditorBot(settings, repository, pipeline, telegram_sources)
    pipeline.set_delivery(editor_bot.deliver_draft)
    await editor_bot.initialize()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    feed_task = asyncio.create_task(rss_worker(settings, repository, pipeline, stop_event))
    telegram_task = asyncio.create_task(
        telegram_source_worker(settings, telegram_sources, stop_event)
    )
    publish_task = asyncio.create_task(editor_bot.scheduled_publish_worker(stop_event))
    try:
        await editor_bot.start_polling()
    finally:
        stop_event.set()
        editor_bot.schedule_wakeup.set()
        feed_task.cancel()
        telegram_task.cancel()
        publish_task.cancel()
        await asyncio.gather(
            feed_task,
            telegram_task,
            publish_task,
            return_exceptions=True,
        )
        await extractor.close()
        await editor_bot.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
