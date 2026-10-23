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
from app.pipeline import ContentPipeline
from app.rss_sources import RssSourceManager
from app.telegram_sources import TelegramSourceManager

logger = logging.getLogger(__name__)


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


async def rss_worker(
    settings: Settings,
    manager: RssSourceManager,
    stop_event: asyncio.Event,
) -> None:
    previous_delay: int | None = None
    while not stop_event.is_set():
        try:
            result = await manager.poll_all()
            if result["found"] or result["errors"]:
                logger.info(
                    "RSS sources checked: sources=%s found=%s processed=%s "
                    "drafts=%s errors=%s",
                    result["sources"],
                    result["found"],
                    result["processed"],
                    result["drafts"],
                    result["errors"],
                )
        except Exception:
            logger.exception("RSS source polling failed")

        delay = rss_poll_delay_seconds(settings)
        if delay != previous_delay:
            logger.info("Next RSS source check in %s seconds", delay)
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


async def draft_cleanup_worker(
    settings: Settings,
    repository: Repository,
    stop_event: asyncio.Event,
) -> None:
    while not stop_event.is_set():
        try:
            cutoff = datetime.now(UTC) - timedelta(
                days=settings.unpublished_retention_days
            )
            deleted_ids = await repository.delete_expired_unpublished_drafts(cutoff)
            if deleted_ids:
                logger.info(
                    "Expired unpublished drafts removed from database: count=%s",
                    len(deleted_ids),
                )
        except Exception:
            logger.exception("Expired draft database cleanup failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.draft_cleanup_interval_seconds,
            )
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
    rss_sources = RssSourceManager(
        repository,
        pipeline,
        timeout=settings.http_timeout_seconds,
        builtin_url=settings.solidot_rss_url,
        bootstrap_items=settings.rss_bootstrap_items,
    )
    await rss_sources.ensure_builtin()
    editor_bot = EditorBot(
        settings,
        repository,
        pipeline,
        telegram_sources,
        rss_sources,
    )
    pipeline.set_delivery(editor_bot.deliver_draft)
    await editor_bot.initialize()

    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_event.set)
        except NotImplementedError:
            pass

    feed_task = asyncio.create_task(rss_worker(settings, rss_sources, stop_event))
    telegram_task = asyncio.create_task(
        telegram_source_worker(settings, telegram_sources, stop_event)
    )
    publish_task = asyncio.create_task(editor_bot.scheduled_publish_worker(stop_event))
    cleanup_task = asyncio.create_task(
        draft_cleanup_worker(settings, repository, stop_event)
    )
    try:
        await editor_bot.start_polling()
    finally:
        stop_event.set()
        editor_bot.schedule_wakeup.set()
        feed_task.cancel()
        telegram_task.cancel()
        publish_task.cancel()
        cleanup_task.cancel()
        await asyncio.gather(
            feed_task,
            telegram_task,
            publish_task,
            cleanup_task,
            return_exceptions=True,
        )
        await extractor.close()
        await editor_bot.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
