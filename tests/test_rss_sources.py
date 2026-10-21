from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.db import Repository
from app.rss import FeedEntry, FeedSnapshot, normalize_rss_url, parse_feed
from app.rss_sources import RssSourceManager


def _entry(external_id: str, title: str = "Story") -> FeedEntry:
    return FeedEntry(
        external_id=external_id,
        title=title,
        link=f"https://news.example/articles/{external_id}",
        summary="Technology story",
        published_at="2026-10-22T10:00:00+00:00",
        urls=[f"https://news.example/articles/{external_id}"],
        raw={
            "id": external_id,
            "media_urls": ["https://news.example/image.jpg"],
        },
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("https://example.com/feed.xml", "https://example.com/feed.xml"),
        ("example.com/rss", "https://example.com/rss"),
        ("http://example.com", "http://example.com/"),
    ],
)
def test_normalize_rss_url(value: str, expected: str) -> None:
    assert normalize_rss_url(value) == expected


@pytest.mark.parametrize(
    "value",
    ["ftp://example.com/feed", "https://user:secret@example.com/feed", "http://x:8080/rss"],
)
def test_normalize_rss_url_rejects_non_public_shapes(value: str) -> None:
    with pytest.raises(ValueError):
        normalize_rss_url(value)


def test_parse_feed_extracts_title_links_and_media() -> None:
    content = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0" xmlns:media="http://search.yahoo.com/mrss/">
      <channel>
        <title>Example Tech</title>
        <item>
          <guid>story-42</guid>
          <title>New device</title>
          <link>https://news.example/story-42</link>
          <description><![CDATA[Read the <a href="https://research.example/report">report</a>.]]></description>
          <media:content url="https://news.example/cover.jpg" type="image/jpeg" />
        </item>
      </channel>
    </rss>"""

    snapshot = parse_feed(content, "https://news.example/feed.xml")

    assert snapshot.title == "Example Tech"
    assert len(snapshot.entries) == 1
    assert snapshot.entries[0].external_id == "story-42"
    assert snapshot.entries[0].urls == [
        "https://news.example/story-42",
        "https://research.example/report",
    ]
    assert snapshot.entries[0].raw["media_urls"] == [
        "https://news.example/cover.jpg"
    ]


@pytest.mark.asyncio
async def test_added_rss_source_baselines_history_and_only_processes_new_items(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = Repository(tmp_path / "editor.sqlite3")
    await repository.initialize()
    pipeline = SimpleNamespace(process_feed_item=AsyncMock(return_value=17))
    manager = RssSourceManager(
        repository,
        pipeline,
        timeout=5,
        builtin_url="https://solidot.example/index.rss",
        bootstrap_items=1,
    )
    initial = FeedSnapshot(
        title="Example Tech",
        url="https://news.example/feed.xml",
        entries=[_entry("42"), _entry("41")],
    )
    monkeypatch.setattr("app.rss_sources.RssFeed.fetch", AsyncMock(return_value=initial))

    source, created = await manager.add("https://news.example/feed.xml")

    assert created
    assert source["kind"] == "rss"
    assert (await repository.get_feed_item_state(source["source_key"], "42"))["status"] == "baseline"

    updated = FeedSnapshot(
        title="Example Tech",
        url="https://news.example/feed.xml",
        entries=[_entry("43", "New story"), _entry("42"), _entry("41")],
    )
    monkeypatch.setattr("app.rss_sources.RssFeed.fetch", AsyncMock(return_value=updated))

    result = await manager.poll_source(source)

    assert result == {"found": 1, "processed": 1, "drafts": 1}
    pipeline.process_feed_item.assert_awaited_once()


def test_failed_rss_item_is_retried_with_delay_and_attempt_limit() -> None:
    old = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()

    assert RssSourceManager._retryable(
        {"status": "failed", "attempt_count": 1, "updated_at": old}
    )
    assert not RssSourceManager._retryable(
        {"status": "failed", "attempt_count": 3, "updated_at": old}
    )
