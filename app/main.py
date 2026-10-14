from __future__ import annotations

import asyncio
import logging
import signal

from app.bot import EditorBot
from app.config import Settings
from app.db import Repository
from app.extractor import ArticleExtractor
from app.llm import QwenClient
from app.models import StoryCandidate
from app.pipeline import ContentPipeline
from app.rss import FeedEntry, SolidotFeed

logger = logging.getLogger(__name__)


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


async def rss_worker(
    settings: Settings,
    repository: Repository,
    pipeline: ContentPipeline,
    stop_event: asyncio.Event,
) -> None:
    feed = SolidotFeed(settings.solidot_rss_url, settings.http_timeout_seconds)
    while not stop_event.is_set():
        try:
            entries = await feed.fetch()
            has_history = await repository.feed_has_items(SolidotFeed.source_key)
            unseen = [
                entry
                for entry in entries
                if not await repository.feed_item_exists(SolidotFeed.source_key, entry.external_id)
            ]
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
        except Exception:
            logger.exception("Solidot RSS polling failed")

        try:
            await asyncio.wait_for(
                stop_event.wait(),
                timeout=settings.rss_poll_interval_seconds,
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
    editor_bot = EditorBot(settings, repository, pipeline)
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
    try:
        await editor_bot.start_polling()
    finally:
        stop_event.set()
        feed_task.cancel()
        await asyncio.gather(feed_task, return_exceptions=True)
        await extractor.close()
        await editor_bot.close()


def run() -> None:
    asyncio.run(main())


if __name__ == "__main__":
    run()
