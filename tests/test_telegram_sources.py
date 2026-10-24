from datetime import UTC, datetime, timedelta

import pytest

from app.telegram_sources import (
    TelegramPublicFeed,
    TelegramSourceManager,
    parse_public_channel_username,
)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("@example_channel", "example_channel"),
        ("https://t.me/example_channel", "example_channel"),
        ("https://t.me/s/example_channel/", "example_channel"),
        ("https://telegram.me/example_channel/42", "example_channel"),
    ],
)
def test_parse_public_channel_username(value: str, expected: str) -> None:
    assert parse_public_channel_username(value) == expected


@pytest.mark.parametrize(
    "value",
    ["short", "https://example.org/channel", "https://t.me/", "bad-name"],
)
def test_parse_public_channel_username_rejects_invalid_values(value: str) -> None:
    if value == "short":
        value = "four"
    with pytest.raises(ValueError):
        parse_public_channel_username(value)


def test_public_telegram_html_is_parsed_without_telegram_links() -> None:
    page = """
    <html><body>
      <div class="tgme_channel_info_header_title"><span>Example News</span></div>
      <div class="tgme_channel_info_header_photo">
        <img src="https://cdn.example.org/channel-avatar.jpg" />
      </div>
      <div class="tgme_widget_message" data-post="example_channel/41">
        <a class="tgme_widget_message_user_photo">
          <img src="https://cdn.example.org/channel-avatar.jpg" />
        </a>
        <div class="tgme_widget_message_text">Old message</div>
        <time datetime="2026-10-22T10:00:00+00:00"></time>
      </div>
      <div class="tgme_widget_message" data-post="example_channel/42">
        <a class="tgme_widget_message_photo_wrap"
           style="background-image:url('https://cdn.example.org/photo.jpg')"></a>
        <div class="tgme_widget_message_text">
          New technology story
          <a href="https://research.example.org/report?utm_source=tg">Research</a>
          <a href="https://t.me/example_channel/40">Telegram</a>
        </div>
        <time datetime="2026-10-22T11:00:00+00:00"></time>
      </div>
    </body></html>
    """

    snapshot = TelegramPublicFeed("example_channel", 5).parse(page)

    assert snapshot.title == "Example News"
    assert snapshot.latest_id == "42"
    assert [entry.external_id for entry in snapshot.entries] == ["41", "42"]
    assert snapshot.entries[0].raw["media_urls"] == []
    assert snapshot.entries[1].urls == ["https://research.example.org/report"]
    assert snapshot.entries[1].raw["media_urls"] == [
        "https://cdn.example.org/photo.jpg"
    ]


def test_failed_telegram_item_is_retried_with_a_delay_and_attempt_limit() -> None:
    old = (datetime.now(UTC) - timedelta(minutes=31)).isoformat()

    assert TelegramSourceManager._retryable(
        {"status": "failed", "attempt_count": 1, "updated_at": old}
    )
    assert not TelegramSourceManager._retryable(
        {"status": "failed", "attempt_count": 3, "updated_at": old}
    )
    assert not TelegramSourceManager._retryable(
        {
            "status": "failed",
            "attempt_count": 1,
            "updated_at": datetime.now(UTC).isoformat(),
        }
    )
