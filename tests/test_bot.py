from datetime import UTC, datetime

import pytest
from aiogram.types import InputMediaPhoto, InputMediaVideo

from app.bot import (
    EditorBot,
    next_publish_slot,
    next_weekly_publish_slot,
    parse_clock_minutes,
    parse_interval_minutes,
    parse_publish_time,
    publish_slot_label,
    upcoming_publish_slots,
)


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
    assert "Контент-план" in labels
    assert "Расписание" in labels
    assert "Канал публикации" in labels
    assert "image:7" in callbacks
    assert "plan:0" in callbacks
    assert "schedule:0" in callbacks
    assert "targetmenu:0" in callbacks
    assert "media:7" not in callbacks


def test_publish_keyboard_offers_now_next_slot_and_custom_time() -> None:
    keyboard = EditorBot._publish_options_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert callbacks == {"pubnow:7", "pubnext:7", "pubtime:7", "pubback:7"}


def test_publish_keyboard_offers_direct_editor_channel_slots() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)
    slots = [
        datetime(2026, 10, 19, 11, 0, tzinfo=UTC),
        datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
    ]

    keyboard = EditorBot._publish_options_keyboard(7, slots=slots, now=now)
    buttons = [button for row in keyboard.inline_keyboard for button in row]

    assert any(button.text == "Сегодня 15:00" for button in buttons)
    assert any(button.text == "Сегодня 18:00" for button in buttons)
    assert "pubslot:7:1792407600" in {button.callback_data for button in buttons}


def test_scheduled_keyboard_can_publish_reschedule_or_cancel() -> None:
    keyboard = EditorBot._scheduled_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert callbacks == {
        "pubnow:7",
        "publish:7",
        "pubunschedule:7",
        "plan:0",
        "schedule:0",
        "targetmenu:0",
    }


def test_content_plan_keyboard_opens_posts_and_channel_settings() -> None:
    keyboard = EditorBot._content_plan_keyboard(
        [{"id": 7, "title": "Запланированный пост"}],
        "IT Vibe News",
    )
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert "planitem:7" in callbacks
    assert "planrefresh:0" in callbacks
    assert "targetmenu:0" in callbacks
    assert "targetadd:0" in callbacks
    assert "schedule:0" in callbacks


def test_schedule_keyboard_has_weekdays_times_and_management_actions() -> None:
    keyboard = EditorBot._schedule_keyboard(
        -1001234567890,
        1,
        [12 * 60 + 10, 20 * 60 + 10],
        "IT Vibe News",
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    callbacks = {button.callback_data for button in buttons}

    assert "🟢 вт" in labels
    assert "✕ 12:10" in labels
    assert "✕ 20:10" in labels
    assert "Настроить интервал" in labels
    assert "scheduleday:-1001234567890:0" in callbacks
    assert "scheduledel:-1001234567890:1:730" in callbacks
    assert "scheduleadd:-1001234567890:1" in callbacks
    assert "schedulereset:-1001234567890:1" in callbacks


def test_content_plan_item_can_edit_and_reschedule_post() -> None:
    keyboard = EditorBot._content_plan_item_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert "planreschedule:7" in callbacks
    assert "title:7" in callbacks
    assert "tags:7" in callbacks
    assert "image:7" in callbacks
    assert "regen:7" in callbacks
    assert "pubunschedule:7" in callbacks


def test_target_only_channel_picker_requests_channel_metadata() -> None:
    keyboard = EditorBot._channel_request_keyboard(target_only=True)
    request = keyboard.keyboard[0][0].request_chat

    assert request is not None
    assert request.request_id == 102
    assert request.request_title is True
    assert request.request_username is True


def test_target_channel_instructions_are_step_by_step() -> None:
    text = EditorBot._target_channel_instructions(include_picker_note=True)

    assert "1. Открой" in text
    assert "@itvibe_editor_bot" in text
    assert "Публикация сообщений" in text
    assert "системный выбор Telegram" in text


def test_parse_publish_time_uses_samara_time_and_rolls_clock_to_tomorrow() -> None:
    now = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)

    scheduled = parse_publish_time("18:30", utc_offset_hours=4, now=now)

    assert scheduled == datetime(2026, 10, 20, 14, 30, tzinfo=UTC)


def test_schedule_clock_parser_accepts_colon_and_compact_formats() -> None:
    assert parse_clock_minutes("03:12") == 192
    assert parse_clock_minutes("0312") == 192
    assert parse_interval_minutes("240") == 240
    assert parse_interval_minutes("04:00") == 240


@pytest.mark.parametrize("value", ["24:00", "1260", "3.12", "nope"])
def test_schedule_clock_parser_rejects_invalid_values(value: str) -> None:
    with pytest.raises(ValueError):
        parse_clock_minutes(value)


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


def test_upcoming_publish_slots_are_unique_and_skip_occupied() -> None:
    now = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)
    occupied = [datetime(2026, 10, 19, 11, 0, tzinfo=UTC)]

    scheduled = upcoming_publish_slots(
        (9 * 60, 15 * 60, 18 * 60),
        occupied,
        utc_offset_hours=4,
        count=3,
        now=now,
    )

    assert scheduled == [
        datetime(2026, 10, 19, 14, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 5, 0, tzinfo=UTC),
        datetime(2026, 10, 20, 11, 0, tzinfo=UTC),
    ]


def test_weekly_publish_slot_uses_only_configured_weekdays() -> None:
    now = datetime(2026, 10, 19, 10, 30, tzinfo=UTC)  # Monday, 14:30 UTC+4
    weekly = {
        0: (12 * 60,),
        1: (9 * 60, 18 * 60),
        2: (),
        3: (),
        4: (),
        5: (),
        6: (),
    }

    scheduled = next_weekly_publish_slot(
        weekly,
        [],
        utc_offset_hours=4,
        now=now,
    )

    assert scheduled == datetime(2026, 10, 20, 5, 0, tzinfo=UTC)


def test_publish_slot_label_uses_local_day() -> None:
    now = datetime(2026, 10, 19, 10, 0, tzinfo=UTC)

    assert publish_slot_label(
        datetime(2026, 10, 20, 5, 0, tzinfo=UTC),
        utc_offset_hours=4,
        now=now,
    ) == "Завтра 09:00"
