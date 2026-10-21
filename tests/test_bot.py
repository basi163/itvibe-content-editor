import json
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiogram.types import InputMediaPhoto, InputMediaVideo

from app.bot import (
    EditorBot,
    next_publish_slot,
    next_weekly_publish_slot,
    parse_editor_post_text,
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
    assert "🗂 Неопубликованные" in labels
    assert "image:7" in callbacks
    assert "plan:0" in callbacks
    assert "schedule:0" in callbacks
    assert "targetmenu:0" in callbacks
    assert "unpublished:0" in callbacks
    assert "home:0" in callbacks
    assert "manualedit:7" in callbacks
    assert "media:7" not in callbacks


def test_publish_keyboard_offers_now_next_slot_and_custom_time() -> None:
    keyboard = EditorBot._publish_options_keyboard(7)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert callbacks == {
        "pubnow:7",
        "pubnext:7",
        "pubtime:7",
        "pubback:7",
        "home:0",
    }


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
        "manualedit:7",
        "unpublished:0",
        "home:0",
    }


def test_home_keyboard_opens_every_main_section() -> None:
    keyboard = EditorBot._home_keyboard(23)
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    callbacks = {button.callback_data for button in buttons}

    assert "🗂 Неопубликованные — 23" in labels
    assert "🔄 Обновить статус" in labels
    assert callbacks == {
        "home:0",
        "unpublished:0",
        "plan:0",
        "schedule:0",
        "targetmenu:0",
        "sources:0",
    }


@pytest.mark.asyncio
async def test_sources_menu_reports_rss_and_telegram_separately() -> None:
    editor = object.__new__(EditorBot)
    editor.repository = SimpleNamespace(
        content_sources=AsyncMock(
            side_effect=[
                [
                    {
                        "id": 1,
                        "kind": "rss",
                        "source_key": "solidot",
                        "title": "Solidot RSS",
                        "url": "https://www.solidot.org/index.rss",
                        "enabled": 1,
                    }
                ],
                [
                    {
                        "id": 2,
                        "kind": "telegram_web",
                        "source_key": "telegram_web:example_channel",
                        "title": "Example Channel",
                        "url": "https://t.me/s/example_channel",
                        "enabled": 1,
                    }
                ],
            ]
        )
    )
    editor._navigation_rows = AsyncMock(return_value=[])
    editor.bot = SimpleNamespace(send_message=AsyncMock())

    await editor._show_sources_menu(-1001, user_id=77)

    text = editor.bot.send_message.await_args.args[1]
    keyboard = editor.bot.send_message.await_args.kwargs["reply_markup"]
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }
    assert "Подключено по RSS: 1" in text
    assert "Solidot RSS" in text
    assert "Подключено по Telegram: 1" in text
    assert "Example Channel (@example_channel)" in text
    assert "sourceaddrss:0" in callbacks
    assert "sourceaddtelegram:0" in callbacks


@pytest.mark.asyncio
async def test_source_check_report_has_separate_rss_and_telegram_blocks() -> None:
    editor = object.__new__(EditorBot)
    editor.rss_sources = SimpleNamespace(
        poll_all=AsyncMock(
            return_value={
                "sources": 2,
                "found": 3,
                "processed": 3,
                "drafts": 1,
                "errors": 0,
            }
        )
    )
    editor.telegram_sources = SimpleNamespace(
        poll_all=AsyncMock(
            return_value={
                "sources": 1,
                "found": 2,
                "processed": 2,
                "drafts": 1,
                "errors": 0,
            }
        )
    )
    editor.repository = SimpleNamespace(
        content_sources=AsyncMock(
            side_effect=[
                [{"title": "Solidot RSS"}, {"title": "Vendor Blog"}],
                [{"title": "Example Channel"}],
            ]
        )
    )
    editor.bot = SimpleNamespace(send_message=AsyncMock())

    await editor._run_source_check(-1001, None)

    text = editor.bot.send_message.await_args.args[1]
    assert "Подключено по RSS:</b> Solidot RSS, Vendor Blog" in text
    assert "Подключено по Telegram:</b> Example Channel" in text
    assert "Всего проверено конвейером: <b>5</b>" in text


@pytest.mark.asyncio
async def test_home_callback_replaces_service_screen_in_place() -> None:
    editor = object.__new__(EditorBot)
    editor._is_owner = AsyncMock(return_value=True)
    editor.repository = SimpleNamespace()
    editor._show_home_menu = AsyncMock()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=77),
        data="home:0",
        message=SimpleNamespace(
            message_id=55,
            chat=SimpleNamespace(id=-1009876543210),
        ),
        answer=AsyncMock(),
    )

    await editor._callback(callback)

    editor._show_home_menu.assert_awaited_once_with(
        -1009876543210,
        message_id=55,
    )


@pytest.mark.asyncio
async def test_home_menu_shows_publication_status_for_each_channel() -> None:
    editor = object.__new__(EditorBot)
    first_at = datetime(2026, 10, 22, 8, 10, tzinfo=UTC)
    second_at = datetime(2026, 10, 22, 16, 10, tzinfo=UTC)
    editor.repository = SimpleNamespace(
        unpublished_draft_count=AsyncMock(return_value=5),
        stats=AsyncMock(return_value={"drafts_published": 17}),
        scheduled_publications_by_channel=AsyncMock(
            return_value={
                -1001: [first_at, second_at],
                -1002: [second_at],
            }
        ),
    )
    editor._publication_channels = AsyncMock(
        return_value=[
            {"chat_id": -1001, "title": "IT Vibe News"},
            {"chat_id": -1002, "title": "Business Vibe"},
        ]
    )
    editor._selected_publication_channel_ids = AsyncMock(
        return_value=[-1001, -1002]
    )
    editor.settings = SimpleNamespace(publish_schedule_utc_offset_hours=4)
    editor.bot = SimpleNamespace(
        edit_message_text=AsyncMock(),
        send_message=AsyncMock(),
    )

    await editor._show_home_menu(-1009876543210)

    text = editor.bot.send_message.await_args.args[1]
    assert "Неопубликовано: <b>5</b>" in text
    assert "Опубликовано постов всего: <b>17</b>" in text
    assert "Запланировано публикаций: <b>3</b>" in text
    assert "Каналов с планом: <b>2</b>" in text
    assert "IT Vibe News" in text
    assert "Business Vibe" in text
    assert "22.10.2026 12:10 (UTC+4)" in text
    assert "Запланировано: <b>2</b>" in text
    assert "Запланировано: <b>1</b>" in text


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
        (1, 3),
        [12 * 60 + 10, 20 * 60 + 10],
        "IT Vibe News",
    )
    buttons = [button for row in keyboard.inline_keyboard for button in row]
    labels = {button.text for button in buttons}
    callbacks = {button.callback_data for button in buttons}

    assert "🟢 вт" in labels
    assert "🟢 чт" in labels
    assert "пн" in labels
    assert "🟢 пн" not in labels
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


def test_target_channel_add_flow_stays_visible_in_editorial_channel() -> None:
    keyboard = EditorBot._target_channel_add_keyboard(-1001234567890)
    callbacks = {
        button.callback_data
        for row in keyboard.inline_keyboard
        for button in row
    }

    assert "targetcurrent:-1001234567890" in callbacks
    assert "targetpicker:0" in callbacks


def test_private_editor_post_url_points_to_exact_message() -> None:
    url = EditorBot._editor_message_url(
        {
            "editor_chat_id": -1001234567890,
            "editor_message_id": 55,
        }
    )

    assert url == "https://t.me/c/1234567890/55"


def test_native_telegram_edit_is_parsed_and_keeps_source_marker() -> None:
    post = parse_editor_post_text(
        "Новый заголовок\n\n"
        "Первый абзац.\n\n"
        "Официальный отчёт подтверждает данные.\n\n"
        "#ИИ #бизнес #технологии\n\n"
        "🔳 IT Vibe News",
        "Первый абзац.\n\n"
        "[[source:0|Официальный отчёт]] подтверждает данные.",
    )

    assert post.title == "Новый заголовок"
    assert post.hashtags == ["#ИИ", "#бизнес", "#технологии"]
    assert "[[source:0|Официальный отчёт]]" in post.body


def test_plain_text_can_be_extracted_from_rich_telegram_blocks() -> None:
    message = SimpleNamespace(
        text=None,
        caption=None,
        rich_message=SimpleNamespace(
            blocks=[
                {"type": "paragraph", "text": {"type": "bold", "text": "Заголовок"}},
                {"type": "paragraph", "text": "Текст новости"},
                {"type": "paragraph", "text": "#ИИ #новости #IT"},
            ]
        ),
    )

    assert EditorBot._editor_message_text(message) == (
        "Заголовок\n\nТекст новости\n\n#ИИ #новости #IT"
    )


@pytest.mark.asyncio
async def test_native_telegram_edit_updates_stored_draft() -> None:
    editor = object.__new__(EditorBot)
    draft = {
        "id": 7,
        "status": "draft",
        "editor_chat_id": -1001234567890,
        "editor_message_id": 55,
        "body": "Старый текст",
        "media": [],
    }
    editor.repository = SimpleNamespace(
        get_draft_by_editor_message=AsyncMock(return_value=draft),
        update_draft=AsyncMock(),
    )
    editor.settings = SimpleNamespace(max_post_chars=2000)
    editor.bot = SimpleNamespace(send_message=AsyncMock())
    message = SimpleNamespace(
        chat=SimpleNamespace(id=-1001234567890),
        message_id=55,
        text=(
            "Новый заголовок\n\nНовый текст публикации.\n\n"
            "#ИИ #новости #IT\n\n🔳 IT Vibe News"
        ),
        caption=None,
        rich_message=None,
        photo=[],
        video=None,
    )

    updated, error = await editor._sync_editor_post(message)

    assert updated
    assert error is None
    fields = editor.repository.update_draft.await_args.kwargs
    assert fields["title"] == "Новый заголовок"
    assert fields["body"] == "Новый текст публикации."
    assert fields["editor_touched_at"]


@pytest.mark.asyncio
async def test_scheduled_publish_reads_current_editor_post_before_sending() -> None:
    editor = object.__new__(EditorBot)
    draft = {
        "id": 31,
        "status": "scheduled",
        "title": "Япония начала охоту за образцами с Фобоса",
        "body": "Текст новости.",
        "hashtags": ["#космос", "#JAXA", "#Марс"],
        "media": [],
        "editor_chat_id": -1001234567890,
        "editor_message_id": 42,
        "editor_media_message_id": None,
    }
    snapshot = SimpleNamespace(
        chat=SimpleNamespace(id=77),
        message_id=999,
        text=(
            "Япония начала охоту за образцами с Фобоса\n\n"
            "Текст новости.\n\n#космос #Марс\n\n🔳 IT Vibe News"
        ),
        caption=None,
        rich_message=None,
        photo=[],
        video=None,
    )
    editor.repository = SimpleNamespace(
        owner_ids=AsyncMock(return_value=[77]),
        update_draft=AsyncMock(),
    )
    editor.settings = SimpleNamespace(max_post_chars=2000)
    editor.bot = SimpleNamespace(
        forward_message=AsyncMock(return_value=snapshot),
        delete_message=AsyncMock(),
        send_message=AsyncMock(),
    )

    synced, error = await editor._sync_current_editor_post(draft)

    assert synced
    assert error is None
    fields = editor.repository.update_draft.await_args.kwargs
    assert json.loads(fields["hashtags_json"]) == ["#космос", "#Марс"]
    editor.bot.forward_message.assert_awaited_once_with(
        chat_id=77,
        from_chat_id=-1001234567890,
        message_id=42,
        disable_notification=True,
    )
    editor.bot.delete_message.assert_awaited_once_with(
        chat_id=77,
        message_id=999,
    )


@pytest.mark.asyncio
async def test_publish_draft_sends_one_edited_post_to_both_selected_channels() -> None:
    editor = object.__new__(EditorBot)
    draft = {
        "id": 7,
        "status": "draft",
        "title": "Новый заголовок",
        "body": "Новый текст публикации.",
        "hashtags": ["#ИИ", "#новости", "#IT"],
        "sources": [],
        "media": [{"type": "photo", "telegram_file_id": "photo-file-id"}],
        "preview_index": 0,
        "publications": [],
    }
    editor.repository = SimpleNamespace(get_draft=AsyncMock(return_value=draft))
    editor.settings = SimpleNamespace(max_post_chars=2000)
    editor.bot = SimpleNamespace(
        send_rich_message=AsyncMock(
            side_effect=[
                SimpleNamespace(message_id=101),
                SimpleNamespace(message_id=202),
            ]
        ),
        send_photo=AsyncMock(),
        send_video=AsyncMock(),
        send_message=AsyncMock(),
    )
    editor._selected_publication_channel_ids = AsyncMock(
        return_value=[-1001, -1002]
    )
    editor._channel_label = AsyncMock(side_effect=["Канал 1", "Канал 2"])
    editor._media_input = AsyncMock(return_value="photo-file-id")
    editor._save_draft_publications = AsyncMock(return_value="published")

    published = await editor._publish_draft_unlocked(7, 100)

    assert published
    assert [
        call.kwargs["chat_id"]
        for call in editor.bot.send_rich_message.await_args_list
    ] == [-1001, -1002]
    final_publications = editor._save_draft_publications.await_args_list[-1].args[1]
    assert {item["status"] for item in final_publications} == {"published"}


def test_parse_publish_time_uses_samara_time_and_rolls_clock_to_tomorrow() -> None:
    now = datetime(2026, 10, 19, 16, 0, tzinfo=UTC)

    scheduled = parse_publish_time("18:30", utc_offset_hours=4, now=now)

    assert scheduled == datetime(2026, 10, 20, 14, 30, tzinfo=UTC)


def test_schedule_picker_is_fully_button_driven() -> None:
    hour_keyboard = EditorBot._schedule_hour_keyboard("i", -1001234567890, 1)
    hour_callbacks = {
        button.callback_data
        for row in hour_keyboard.inline_keyboard
        for button in row
    }
    minute_keyboard = EditorBot._schedule_minute_keyboard(
        "i",
        -1001234567890,
        1,
        9,
        3,
    )
    minute_callbacks = {
        button.callback_data
        for row in minute_keyboard.inline_keyboard
        for button in row
    }
    count_keyboard = EditorBot._schedule_count_keyboard(-1001234567890, 1, 570)
    count_callbacks = {
        button.callback_data
        for row in count_keyboard.inline_keyboard
        for button in row
    }

    assert "schedulehour:i:-1001234567890:1:9" in hour_callbacks
    assert "scheduleminute:i:-1001234567890:1:9:30" in minute_callbacks
    assert "schedulecount:-1001234567890:1:570:12" in count_callbacks
    assert "home:0" in hour_callbacks
    assert "home:0" in minute_callbacks
    assert "home:0" in count_callbacks


def test_schedule_interval_picker_offers_presets_and_custom_values() -> None:
    gap_keyboard = EditorBot._schedule_gap_keyboard(-1001234567890, 1, 570, 3)
    gap_callbacks = {
        button.callback_data
        for row in gap_keyboard.inline_keyboard
        for button in row
    }
    custom_keyboard = EditorBot._schedule_gap_minute_keyboard(
        -1001234567890,
        1,
        570,
        3,
        2,
    )
    custom_callbacks = {
        button.callback_data
        for row in custom_keyboard.inline_keyboard
        for button in row
    }

    assert "scheduleintgap:-1001234567890:1:570:3:120" in gap_callbacks
    assert "schedulegapcustom:-1001234567890:1:570:3" in gap_callbacks
    assert "schedulegapminute:-1001234567890:1:570:3:2:15" in custom_callbacks
    assert "home:0" in gap_callbacks
    assert "home:0" in custom_callbacks
    assert all(len(value or "") <= 64 for value in gap_callbacks | custom_callbacks)


@pytest.mark.asyncio
async def test_schedule_interval_callback_stays_in_editorial_chat() -> None:
    editor = object.__new__(EditorBot)
    editor._is_owner = AsyncMock(return_value=True)
    editor.repository = SimpleNamespace(pop_pending_action=AsyncMock())
    editor._channel_enabled_days = AsyncMock(return_value=(1, 3))
    editor._show_schedule_hour_picker = AsyncMock()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=77),
        data="scheduleinterval:-1001234567890:1",
        message=SimpleNamespace(
            message_id=55,
            chat=SimpleNamespace(id=-1009876543210),
        ),
        answer=AsyncMock(),
    )

    await editor._callback(callback)

    editor._show_schedule_hour_picker.assert_awaited_once_with(
        -1009876543210,
        55,
        -1001234567890,
        1,
        "i",
    )


@pytest.mark.asyncio
async def test_weekday_button_adds_day_to_multi_selection() -> None:
    editor = object.__new__(EditorBot)
    editor._is_owner = AsyncMock(return_value=True)
    editor._channel_enabled_days = AsyncMock(return_value=(0, 4))
    editor._set_channel_enabled_days = AsyncMock()
    editor._show_schedule_menu = AsyncMock()
    callback = SimpleNamespace(
        from_user=SimpleNamespace(id=77),
        data="scheduleday:-1001234567890:2",
        message=SimpleNamespace(
            message_id=55,
            chat=SimpleNamespace(id=-1009876543210),
        ),
        answer=AsyncMock(),
    )

    await editor._callback(callback)

    editor._set_channel_enabled_days.assert_awaited_once_with(
        -1001234567890,
        {0, 2, 4},
    )
    editor._show_schedule_menu.assert_awaited_once_with(
        -1009876543210,
        -1001234567890,
        message_id=55,
        user_id=77,
    )


@pytest.mark.asyncio
async def test_disabled_weekdays_are_excluded_from_publication_slots() -> None:
    editor = object.__new__(EditorBot)
    editor._channel_stored_weekly_schedule = AsyncMock(
        return_value={day: (9 * 60,) for day in range(7)}
    )
    editor._channel_enabled_days = AsyncMock(return_value=(1, 3, 5))

    weekly = await editor._channel_weekly_schedule(-1001234567890)

    assert weekly[0] == ()
    assert weekly[1] == (540,)
    assert weekly[2] == ()
    assert weekly[3] == (540,)


@pytest.mark.asyncio
async def test_equal_schedule_rejects_rollover_to_next_day() -> None:
    editor = object.__new__(EditorBot)
    editor._set_channel_days_schedule = AsyncMock()

    error = await editor._save_equal_schedule(-1001234567890, 1, 23 * 60, 2, 60)

    assert "следующий день" in str(error)
    editor._set_channel_days_schedule.assert_not_awaited()


@pytest.mark.asyncio
async def test_equal_schedule_applies_to_every_enabled_day() -> None:
    editor = object.__new__(EditorBot)
    editor._channel_enabled_days = AsyncMock(return_value=(0, 2, 4))
    editor._set_channel_days_schedule = AsyncMock()

    error = await editor._save_equal_schedule(-1001234567890, 0, 9 * 60, 3, 120)

    assert error is None
    editor._set_channel_days_schedule.assert_awaited_once_with(
        -1001234567890,
        (0, 2, 4),
        [540, 660, 780],
    )


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
