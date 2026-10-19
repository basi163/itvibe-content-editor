from datetime import UTC, datetime

import pytest
from aiogram.types import InputMediaPhoto, InputMediaVideo

from app.bot import EditorBot, next_publish_slot, parse_publish_time


def test_rich_photo_is_embedded_in_one_message() -> None:
    message = EditorBot._rich_message("<h2>Title</h2><p>Body</p>", "photo-file-id", "photo")

    assert message.html == '<img src="tg://photo?id=cover"/><h2>Title</h2><p>Body</p>'
    assert message.media is not None
    assert len(message.media) == 1
    assert isinstance(message.media[0].media, InputMediaPhoto)


def test_rich_video_is_embedded_in_one_message() -> None:
    message = EditorBot._rich_message("<p>Body</p>", "video-file-id", "video")

    assert message.html == '<video src="tg://video?id=cover"></video><p>Body</p>'
    assert message.media is not None
    assert isinstance(message.media[0].media, InputMediaVideo)


def test_draft_keyboard_describes_automatic_edits() -> None:
    keyboard = EditorBot._draft_keyboard(7)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    callbacks = {button.callback_data for button in buttons}

    assert "Другой заголовок" in labels
    assert "Другие хэштеги" in labels
    assert "Другое изображение" in labels
    assert "image:7" in callbacks
    assert "media:7" not in callbacks


def test_publish_keyboard_offers_now_next_slot_and_custom_time() -> None:
    keyboard = EditorBot._publish_options_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert callbacks == {"pubnow:7", "pubnext:7", "pubtime:7"}


def test_scheduled_keyboard_can_publish_reschedule_or_cancel() -> None:
    keyboard = EditorBot._scheduled_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert callbacks == {"pubnow:7", "pubtime:7", "pubunschedule:7"}


def test_parse_publish_time_uses_samara_time_and_rolls_clock_to_tomorrow() -> None:
    now = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)

    scheduled = parse_publish_time("18:30", utc_offset_hours=4, now=now)

    assert scheduled == datetime(2026, 10, 20, 14, 30, tzinfo=UTC)


def test_parse_publish_time_accepts_tomorrow_and_full_date() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)

    tomorrow = parse_publish_time("завтра 09:15", utc_offset_hours=4, now=now)
    explicit = parse_publish_time(
        "25.10.2026 12:00",
        utc_offset_hours=4,
        now=now,
    )

    assert tomorrow == datetime(2026, 10, 20, 5, 15, tzinfo=UTC)
    assert explicit == datetime(2026, 10, 25, 8, 0, tzinfo=UTC)


def test_parse_publish_time_rejects_explicit_past_time() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="в будущем"):
        parse_publish_time("сегодня 10:00", utc_offset_hours=4, now=now)


def test_next_publish_slot_skips_past_and_occupied_slots() -> None:
    now = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)
    occupied = [datetime(2026, 10, 19, 11, 0, tzinfo=UTC)]

    scheduled = next_publish_slot(
        (9 * 60, 15 * 60, 18 * 60),
        occupied,
        utc_offset_hours=4,
        now=now,
    )

    assert scheduled == datetime(2026, 10, 19, 14, 0, tzinfo=UTC)
