from __future__ import annotations

import asyncio
import html
import json
import logging
import re
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus, ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import (
    BotCommand,
    BufferedInputFile,
    CallbackQuery,
    ChatShared,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    InputMediaVideo,
    InputRichMessage,
    InputRichMessageMedia,
    KeyboardButton,
    KeyboardButtonRequestChat,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

from app.config import Settings
from app.db import Repository
from app.models import GeneratedPost, SourceDocument, StoryCandidate
from app.pipeline import ContentPipeline
from app.renderer import EMOJI_RE, render_post, validate_post

logger = logging.getLogger(__name__)
URL_RE = re.compile(r"https?://[^\s<>]+", re.IGNORECASE)
EDITOR_REQUEST_ID = 101
TARGET_REQUEST_ID = 102
SCHEDULE_RETRY_DELAY = timedelta(minutes=5)
MAX_SCHEDULE_ATTEMPTS = 3
PUBLICATION_CHANNELS_SETTING = "publication_channels"


def _publish_timezone(utc_offset_hours: int) -> timezone:
    return timezone(timedelta(hours=utc_offset_hours))


def format_publish_time(value: datetime, utc_offset_hours: int) -> str:
    local = value.astimezone(_publish_timezone(utc_offset_hours))
    sign = "+" if utc_offset_hours >= 0 else ""
    return f"{local:%d.%m.%Y %H:%M} (UTC{sign}{utc_offset_hours})"


def parse_publish_time(
    value: str,
    *,
    utc_offset_hours: int,
    now: datetime | None = None,
) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    local_tz = _publish_timezone(utc_offset_hours)
    local_now = current.astimezone(local_tz)
    cleaned = re.sub(r"\s+", " ", value.strip().casefold())
    day_shift: int | None = None
    if cleaned.startswith("сегодня "):
        day_shift = 0
        cleaned = cleaned.removeprefix("сегодня ").strip()
    elif cleaned.startswith("завтра "):
        day_shift = 1
        cleaned = cleaned.removeprefix("завтра ").strip()

    candidate: datetime | None = None
    if day_shift is not None:
        try:
            parsed_time = datetime.strptime(cleaned, "%H:%M").time()
        except ValueError as error:
            raise ValueError("Время нужно указать в формате ЧЧ:ММ") from error
        target_date = local_now.date() + timedelta(days=day_shift)
        candidate = datetime.combine(target_date, parsed_time, tzinfo=local_tz)
    else:
        for pattern in ("%d.%m.%Y %H:%M", "%Y-%m-%d %H:%M"):
            try:
                candidate = datetime.strptime(cleaned, pattern).replace(tzinfo=local_tz)
                break
            except ValueError:
                pass
        if candidate is None:
            try:
                parsed = datetime.strptime(cleaned, "%d.%m %H:%M")
            except ValueError:
                parsed = None
            if parsed is not None:
                candidate = parsed.replace(year=local_now.year, tzinfo=local_tz)
                if candidate <= local_now:
                    candidate = candidate.replace(year=local_now.year + 1)
        if candidate is None:
            try:
                parsed_time = datetime.strptime(cleaned, "%H:%M").time()
            except ValueError as error:
                raise ValueError(
                    "Используй ЧЧ:ММ, сегодня ЧЧ:ММ, завтра ЧЧ:ММ "
                    "или ДД.ММ.ГГГГ ЧЧ:ММ"
                ) from error
            candidate = datetime.combine(local_now.date(), parsed_time, tzinfo=local_tz)
            if candidate <= local_now:
                candidate += timedelta(days=1)

    if candidate <= local_now:
        raise ValueError("Время публикации должно быть в будущем")
    return candidate.astimezone(UTC)


def next_publish_slot(
    slots: tuple[int, ...],
    occupied: list[datetime],
    *,
    utc_offset_hours: int,
    now: datetime | None = None,
) -> datetime:
    current = (now or datetime.now(UTC)).astimezone(UTC)
    local_tz = _publish_timezone(utc_offset_hours)
    local_now = current.astimezone(local_tz)
    occupied_minutes = {
        int(value.astimezone(UTC).timestamp() // 60)
        for value in occupied
    }
    for day_shift in range(367):
        target_date = local_now.date() + timedelta(days=day_shift)
        for minute_of_day in slots:
            candidate = datetime(
                target_date.year,
                target_date.month,
                target_date.day,
                minute_of_day // 60,
                minute_of_day % 60,
                tzinfo=local_tz,
            ).astimezone(UTC)
            if candidate <= current:
                continue
            if int(candidate.timestamp() // 60) not in occupied_minutes:
                return candidate
    raise RuntimeError("Не удалось найти свободное окно публикации")


def upcoming_publish_slots(
    slots: tuple[int, ...],
    occupied: list[datetime],
    *,
    utc_offset_hours: int,
    count: int = 6,
    now: datetime | None = None,
) -> list[datetime]:
    upcoming: list[datetime] = []
    reserved = list(occupied)
    for _ in range(count):
        candidate = next_publish_slot(
            slots,
            reserved,
            utc_offset_hours=utc_offset_hours,
            now=now,
        )
        upcoming.append(candidate)
        reserved.append(candidate)
    return upcoming


def publish_slot_label(
    value: datetime,
    *,
    utc_offset_hours: int,
    now: datetime | None = None,
) -> str:
    local_tz = _publish_timezone(utc_offset_hours)
    current_date = (now or datetime.now(UTC)).astimezone(local_tz).date()
    local = value.astimezone(local_tz)
    if local.date() == current_date:
        prefix = "Сегодня"
    elif local.date() == current_date + timedelta(days=1):
        prefix = "Завтра"
    else:
        prefix = f"{local:%d.%m}"
    return f"{prefix} {local:%H:%M}"


class EditorBot:
    def __init__(
        self,
        settings: Settings,
        repository: Repository,
        pipeline: ContentPipeline,
    ) -> None:
        self.settings = settings
        self.repository = repository
        self.pipeline = pipeline
        self.bot = Bot(
            token=settings.telegram_bot_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self.dispatcher = Dispatcher()
        self.router = Router()
        self.schedule_wakeup = asyncio.Event()
        self._publication_locks: dict[int, asyncio.Lock] = {}
        self.dispatcher.include_router(self.router)
        self._register_handlers()

    async def initialize(self) -> None:
        await self.repository.clear_pending_actions()
        await self.bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить редактор"),
                BotCommand(command="setup", description="Привязать каналы"),
                BotCommand(command="plan", description="Контент-план"),
                BotCommand(command="status", description="Показать состояние системы"),
                BotCommand(command="sources", description="Показать источники"),
                BotCommand(command="settings", description="Настройки генерации"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        if self.settings.editor_chat_id and not await self.repository.get_setting(
            "editor_chat_id"
        ):
            await self.repository.set_setting("editor_chat_id", str(self.settings.editor_chat_id))
        if self.settings.target_chat_id and not await self.repository.get_setting(
            "target_chat_id"
        ):
            await self.repository.set_setting("target_chat_id", str(self.settings.target_chat_id))
        target_chat_id = await self.repository.get_setting("target_chat_id")
        known_channels = await self._publication_channels()
        if target_chat_id and not any(
            str(channel["chat_id"]) == target_chat_id for channel in known_channels
        ):
            try:
                target_chat = await self.bot.get_chat(int(target_chat_id))
            except TelegramBadRequest:
                logger.warning("Could not load current publication channel metadata")
            else:
                await self._remember_publication_channel(
                    int(target_chat_id),
                    str(target_chat.title or target_chat_id),
                    target_chat.username,
                )

    async def start_polling(self) -> None:
        await self.dispatcher.start_polling(self.bot, allowed_updates=self.dispatcher.resolve_used_update_types())

    async def close(self) -> None:
        await self.bot.session.close()

    def _register_handlers(self) -> None:
        self.router.message.register(self._start, Command("start"))
        self.router.message.register(self._setup, Command("setup"))
        self.router.message.register(self._plan, Command("plan"))
        self.router.message.register(self._status, Command("status"))
        self.router.message.register(self._sources, Command("sources"))
        self.router.message.register(self._settings, Command("settings"))
        self.router.message.register(self._help, Command("help"))
        self.router.message.register(self._chat_shared, F.chat_shared)
        self.router.callback_query.register(self._callback, F.data)
        self.router.message.register(self._private_message, F.chat.type == ChatType.PRIVATE)

    async def _is_owner(self, user_id: int) -> bool:
        return await self.repository.is_owner(user_id)

    async def _publication_channels(self) -> list[dict[str, Any]]:
        raw = await self.repository.get_setting(PUBLICATION_CHANNELS_SETTING)
        if not raw:
            return []
        try:
            values = json.loads(raw)
        except (TypeError, ValueError):
            return []
        return [value for value in values if isinstance(value, dict) and value.get("chat_id")]

    async def _remember_publication_channel(
        self,
        chat_id: int,
        title: str,
        username: str | None,
    ) -> None:
        channels = await self._publication_channels()
        channel = {
            "chat_id": chat_id,
            "title": title or str(chat_id),
            "username": username,
        }
        channels = [item for item in channels if int(item["chat_id"]) != chat_id]
        channels.append(channel)
        await self.repository.set_setting(
            PUBLICATION_CHANNELS_SETTING,
            json.dumps(channels, ensure_ascii=False),
        )

    async def _target_channel_label(self) -> str:
        target = await self.repository.get_setting("target_chat_id")
        if not target:
            return "не выбран"
        for channel in await self._publication_channels():
            if str(channel["chat_id"]) == target:
                return str(channel.get("title") or channel.get("username") or target)
        return target

    async def _start(self, message: Message) -> None:
        if not message.from_user:
            return
        claimed = await self.repository.claim_owner(message.from_user.id)
        if not claimed and not await self._is_owner(message.from_user.id):
            await message.answer("Бот уже привязан к владельцу.")
            return
        text = (
            "IT Vibe Content Editor запущен.\n\n"
            "Бот получает новости из Solidot RSS и пересланные сообщения, проверяет "
            "источники и готовит черновики. Для привязки каналов используй /setup."
        )
        await message.answer(text)

    async def _setup(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        await message.answer(
            "Сначала добавь бота администратором нужного канала, затем выбери канал.",
            reply_markup=self._channel_request_keyboard(),
        )

    @staticmethod
    def _channel_request_keyboard(*, target_only: bool = False) -> ReplyKeyboardMarkup:
        rows: list[list[KeyboardButton]] = []
        if not target_only:
            rows.append(
                [
                    KeyboardButton(
                        text="Выбрать редакторский канал",
                        request_chat=KeyboardButtonRequestChat(
                            request_id=EDITOR_REQUEST_ID,
                            chat_is_channel=True,
                            bot_is_member=True,
                        ),
                    )
                ]
            )
        rows.append(
            [
                KeyboardButton(
                    text="Выбрать канал публикации",
                    request_chat=KeyboardButtonRequestChat(
                        request_id=TARGET_REQUEST_ID,
                        chat_is_channel=True,
                        bot_is_member=True,
                        request_title=True,
                        request_username=True,
                    ),
                )
            ]
        )
        return ReplyKeyboardMarkup(
            keyboard=rows,
            resize_keyboard=True,
            one_time_keyboard=True,
        )

    async def _plan(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        await self._show_content_plan(message.chat.id)

    async def _chat_shared(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        shared: ChatShared = message.chat_shared
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(shared.chat_id, me.id)
            selected_chat = await self.bot.get_chat(shared.chat_id)
        except TelegramBadRequest:
            await message.answer("Не удалось проверить канал. Добавь бота в него администратором.")
            return
        if member.status not in {ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.CREATOR}:
            await message.answer("Бот должен быть администратором выбранного канала.")
            return

        if shared.request_id == EDITOR_REQUEST_ID:
            await self.repository.set_setting("editor_chat_id", str(shared.chat_id))
            await message.answer(
                "Редакторский канал привязан.",
                reply_markup=ReplyKeyboardRemove(),
            )
            for draft in reversed(await self.repository.recent_drafts(10)):
                if draft["status"] == "draft" and not draft["editor_message_id"]:
                    await self.deliver_draft(int(draft["id"]))
        elif shared.request_id == TARGET_REQUEST_ID:
            await self.repository.set_setting("target_chat_id", str(shared.chat_id))
            await self._remember_publication_channel(
                shared.chat_id,
                str(selected_chat.title or shared.title or shared.chat_id),
                selected_chat.username or shared.username,
            )
            await message.answer(
                f"Канал публикации переключён на "
                f"<b>{html.escape(str(selected_chat.title or shared.chat_id))}</b>.",
                reply_markup=ReplyKeyboardRemove(),
            )

    async def _status(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        stats = await self.repository.stats()
        editor = await self.repository.get_setting("editor_chat_id") or "не привязан"
        target = await self._target_channel_label()
        next_at = await self.repository.next_scheduled_publication()
        next_text = (
            format_publish_time(
                next_at,
                self.settings.publish_schedule_utc_offset_hours,
            )
            if next_at
            else "нет"
        )
        await message.answer(
            "Сервис работает.\n\n"
            f"Получено материалов  {stats.get('feed_items', 0)}\n"
            f"Создано черновиков  {stats.get('drafts', 0)}\n"
            f"Опубликовано  {stats.get('drafts_published', 0)}\n"
            f"Запланировано  {stats.get('drafts_scheduled', 0)}\n"
            f"Ближайшая публикация  {next_text}\n"
            f"Редакторский канал  <code>{html.escape(editor)}</code>\n"
            f"Канал публикации  <code>{html.escape(target)}</code>"
        )

    async def _sources(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        await message.answer(
            "Активный источник\n\n"
            f"Solidot RSS\n<code>{html.escape(self.settings.solidot_rss_url)}</code>\n\n"
            "Также можно переслать боту любой доступный пост из другого канала."
        )

    async def _settings(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        publish_slots = ", ".join(
            f"{slot // 60:02d}:{slot % 60:02d}"
            for slot in self.settings.publish_schedule_slots
        )
        offset = self.settings.publish_schedule_utc_offset_hours
        offset_text = f"UTC{'+' if offset >= 0 else ''}{offset}"
        await message.answer(
            f"Лимит поста  {self.settings.max_post_chars}\n"
            f"Порог релевантности  {self.settings.relevance_threshold:.2f}\n"
            "Проверка RSS  адаптивно\n"
            f"В активные часы  каждые {self.settings.rss_poll_interval_seconds} секунд\n"
            "Вне активных часов  каждые "
            f"{self.settings.rss_offpeak_poll_interval_seconds} секунд\n"
            f"Окна пубикации  {publish_slots} ({offset_text})\n"
            f"Модель отбора  <code>{html.escape(self.settings.triage_model)}</code>\n"
            f"Модель текста  <code>{html.escape(self.settings.writer_model)}</code>"
        )

    async def _show_content_plan(
        self,
        chat_id: int,
        message_id: int | None = None,
    ) -> None:
        drafts = await self.repository.scheduled_drafts()
        target_label = await self._target_channel_label()
        lines = [
            "<b>Контент-план</b>",
            f"Канал публикации  <b>{html.escape(target_label)}</b>",
            "",
        ]
        if drafts:
            for draft in drafts:
                scheduled_at = self._draft_scheduled_at(draft)
                when = (
                    format_publish_time(
                        scheduled_at,
                        self.settings.publish_schedule_utc_offset_hours,
                    )
                    if scheduled_at
                    else "время не указано"
                )
                lines.append(
                    f"<b>#{draft['id']}</b>  {when}\n"
                    f"{html.escape(str(draft['title']))}"
                )
        else:
            lines.append("Запланированных постов пока нет.")
        text = "\n\n".join(lines)
        keyboard = self._content_plan_keyboard(drafts, target_label)
        if message_id is not None:
            try:
                await self.bot.edit_message_text(
                    text=text,
                    chat_id=chat_id,
                    message_id=message_id,
                    reply_markup=keyboard,
                )
                return
            except TelegramBadRequest:
                logger.info("Could not refresh content plan message; sending a new one")
        await self.bot.send_message(chat_id, text, reply_markup=keyboard)

    async def _show_plan_item(
        self,
        chat_id: int,
        message_id: int,
        draft: dict[str, Any],
    ) -> None:
        scheduled_at = self._draft_scheduled_at(draft)
        when = (
            format_publish_time(
                scheduled_at,
                self.settings.publish_schedule_utc_offset_hours,
            )
            if scheduled_at
            else "не запланировано"
        )
        target_label = await self._target_channel_label()
        text = (
            f"<b>Контент-план · пост #{draft['id']}</b>\n\n"
            f"<b>{html.escape(str(draft['title']))}</b>\n\n"
            f"Публикация  {when}\n"
            f"Канал  <b>{html.escape(target_label)}</b>"
        )
        await self.bot.edit_message_text(
            text=text,
            chat_id=chat_id,
            message_id=message_id,
            reply_markup=self._content_plan_item_keyboard(int(draft["id"])),
        )

    async def _show_target_channel_menu(self, chat_id: int) -> None:
        channels = await self._publication_channels()
        current = await self.repository.get_setting("target_chat_id")
        rows: list[list[InlineKeyboardButton]] = []
        for channel in channels:
            channel_id = int(channel["chat_id"])
            title = str(channel.get("title") or channel.get("username") or channel_id)
            marker = "✅ " if str(channel_id) == current else ""
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{marker}{title}"[:60],
                        callback_data=f"target:{channel_id}",
                    )
                ]
            )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="Добавить другой канал",
                        callback_data="targetadd:0",
                    )
                ],
                [
                    InlineKeyboardButton(
                        text="Контент-план",
                        callback_data="plan:0",
                    )
                ],
            ]
        )
        await self.bot.send_message(
            chat_id,
            "<b>Канал итоговой публикации</b>\n\n"
            "Выбери добавленный канал или добавь новый.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=rows),
        )

    async def _send_target_channel_picker(self, user_id: int) -> None:
        await self.bot.send_message(
            user_id,
            "Добавь бота администратором нужного канала и выбери его кнопкой ниже.",
            reply_markup=self._channel_request_keyboard(target_only=True),
        )

    @staticmethod
    def _draft_scheduled_at(draft: dict[str, Any]) -> datetime | None:
        value = draft.get("scheduled_at")
        if not value:
            return None
        try:
            scheduled_at = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        if scheduled_at.tzinfo is None:
            scheduled_at = scheduled_at.replace(tzinfo=UTC)
        return scheduled_at.astimezone(UTC)

    @staticmethod
    def _content_plan_keyboard(
        drafts: list[dict[str, Any]],
        target_label: str,
    ) -> InlineKeyboardMarkup:
        rows = [
            [
                InlineKeyboardButton(
                    text=f"#{draft['id']}  {str(draft['title'])[:42]}",
                    callback_data=f"planitem:{draft['id']}",
                )
            ]
            for draft in drafts[:40]
        ]
        rows.extend(
            [
                [
                    InlineKeyboardButton(text="Обновить", callback_data="planrefresh:0"),
                    InlineKeyboardButton(
                        text=f"Канал: {target_label}"[:45],
                        callback_data="targetmenu:0",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Добавить/сменить канал",
                        callback_data="targetadd:0",
                    )
                ],
            ]
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    @staticmethod
    def _content_plan_item_keyboard(draft_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Изменить время",
                        callback_data=f"planreschedule:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Опубликовать сейчас",
                        callback_data=f"pubnow:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Другой заголовок",
                        callback_data=f"title:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Другие хэштеги",
                        callback_data=f"tags:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Другое изображение",
                        callback_data=f"image:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Переписать всё",
                        callback_data=f"regen:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Снять с расписания",
                        callback_data=f"pubunschedule:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Канал публикации",
                        callback_data="targetmenu:0",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Назад к плану",
                        callback_data="planrefresh:0",
                    )
                ],
            ]
        )

    async def _help(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        await message.answer(
            "Перешли сюда пост из любого канала или дождись новой публикации Solidot. "
            "Подготовленный материал появится в редакторском канале. Управление выполняется "
            "кнопками под черновиком. Очередь публикаций и её настройки доступны "
            "через /plan и кнопку «Контент-план»."
        )

    async def _private_message(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        if message.text and message.text.startswith("/"):
            return

        pending = await self.repository.pop_pending_action(message.from_user.id)
        if pending:
            action, draft_id = pending
            await self._apply_pending_edit(message, action, draft_id)
            return

        if not message.forward_origin:
            await message.answer("Перешли пост из канала или используй команды меню.")
            return
        candidate = self._candidate_from_message(message)
        await message.answer("Материал принят. Проверяю источники и релевантность.")

        async def process() -> None:
            draft_id = await self.pipeline.process_manual(candidate)
            if draft_id:
                await message.answer(f"Черновик #{draft_id} подготовлен.")
            else:
                await message.answer("Материал отклонен фильтром или не удалось его обработать.")

        asyncio.create_task(process())

    async def _apply_pending_edit(self, message: Message, action: str, draft_id: int) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            await message.answer("Черновик не найден.")
            return
        if action == "schedule_time":
            text = (message.text or message.caption or "").strip()
            try:
                scheduled_at = parse_publish_time(
                    text,
                    utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
                )
            except ValueError as error:
                await self.repository.set_pending_action(message.from_user.id, action, draft_id)
                await message.answer(f"{error}. Попробуй еще раз.")
                return
            await self.schedule_draft(draft_id, message.from_user.id, scheduled_at)
            return
        if action == "media":
            file_id = None
            media_type = None
            if message.photo:
                file_id = message.photo[-1].file_id
                media_type = "photo"
            elif message.video:
                file_id = message.video.file_id
                media_type = "video"
            if not file_id or not media_type:
                await self.repository.set_pending_action(message.from_user.id, action, draft_id)
                await message.answer("Пришли фотографию или видео одним сообщением.")
                return
            custom_media = {
                "type": media_type,
                "telegram_file_id": file_id,
                "page_url": None,
                "image_url": None,
            }
            media = [custom_media, *draft["media"]]
            await self.repository.update_draft(
                draft_id,
                media_json=json.dumps(media, ensure_ascii=False),
                preview_index=0,
            )
            await self.refresh_draft(
                draft_id,
                replace_message=True,
            )
            await message.answer("Медиа сохранено.")
            return
        text = (message.text or message.caption or "").strip()
        if action == "title":
            if not text or ":" in text or "：" in text or EMOJI_RE.search(text):
                await self.repository.set_pending_action(message.from_user.id, action, draft_id)
                await message.answer("Заголовок пустой либо содержит двоеточие или эмодзи. Пришли другой.")
                return
            post = GeneratedPost(text, draft["body"], draft["hashtags"])
            errors = validate_post(post, self.settings.max_post_chars)
            if errors:
                await self.repository.set_pending_action(message.from_user.id, action, draft_id)
                await message.answer("Не удалось применить заголовок. " + "; ".join(errors))
                return
            await self.repository.update_draft(draft_id, title=text)
        elif action == "hashtags":
            tags = [part.strip() for part in re.split(r"[\s,]+", text) if part.strip()]
            tags = [tag if tag.startswith("#") else f"#{tag}" for tag in tags]
            post = GeneratedPost(draft["title"], draft["body"], tags)
            errors = validate_post(post, self.settings.max_post_chars)
            if errors:
                await self.repository.set_pending_action(message.from_user.id, action, draft_id)
                await message.answer("Не удалось применить хэштеги. " + "; ".join(errors))
                return
            await self.repository.update_draft(
                draft_id,
                hashtags_json=json.dumps(tags, ensure_ascii=False),
            )
        await self.refresh_draft(draft_id)
        await message.answer("Изменения сохранены.")

    async def _callback(self, callback: CallbackQuery) -> None:
        if not callback.from_user or not await self._is_owner(callback.from_user.id):
            await callback.answer("Недостаточно прав", show_alert=True)
            return
        parts = (callback.data or "").split(":")
        action = parts[0]
        is_slot_action = action == "pubslot"
        if is_slot_action:
            valid = len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit()
        elif action == "target":
            valid = len(parts) == 2 and parts[1].lstrip("-").isdigit()
        else:
            valid = len(parts) == 2 and parts[1].isdigit()
        if not valid:
            await callback.answer("Некорректная команда", show_alert=True)
            return

        if action in {"plan", "planrefresh"}:
            await callback.answer("Открываю контент-план")
            if callback.message:
                message_id = (
                    callback.message.message_id
                    if action == "planrefresh"
                    else None
                )
                await self._show_content_plan(callback.message.chat.id, message_id)
            return
        if action == "targetmenu":
            await callback.answer("Выбери канал")
            if callback.message:
                await self._show_target_channel_menu(callback.message.chat.id)
            return
        if action == "targetadd":
            await callback.answer("Выбор канала отправлен в бот")
            await self._send_target_channel_picker(callback.from_user.id)
            return
        if action == "target":
            channel_id = int(parts[1])
            channels = await self._publication_channels()
            selected = next(
                (
                    channel
                    for channel in channels
                    if int(channel["chat_id"]) == channel_id
                ),
                None,
            )
            if not selected:
                await callback.answer("Канал не найден", show_alert=True)
                return
            await self.repository.set_setting("target_chat_id", str(channel_id))
            title = str(selected.get("title") or selected.get("username") or channel_id)
            await callback.answer(f"Выбран канал {title}", show_alert=True)
            if callback.message:
                await self._show_content_plan(callback.message.chat.id)
            return

        draft_id = int(parts[1])
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            await callback.answer("Черновик не найден", show_alert=True)
            return

        if action == "pubslot":
            try:
                scheduled_at = datetime.fromtimestamp(int(parts[2]), UTC)
            except (OverflowError, OSError, ValueError):
                await callback.answer("Некорректное время", show_alert=True)
                return
            await callback.answer("Ставлю в расписание")
            await self.schedule_draft(draft_id, callback.from_user.id, scheduled_at)
            await self._refresh_plan_from_callback(callback)
            return
        if action == "pubnow":
            await callback.answer("Публикую сейчас")
            await self.publish_draft(draft_id, callback.from_user.id)
            await self._refresh_plan_from_callback(callback)
            return
        if action == "pubnext":
            await callback.answer("Ставлю в расписание")
            occupied = await self.repository.scheduled_publication_times()
            scheduled_at = next_publish_slot(
                self.settings.publish_schedule_slots,
                occupied,
                utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
            )
            await self.schedule_draft(draft_id, callback.from_user.id, scheduled_at)
            await self._refresh_plan_from_callback(callback)
            return
        if action == "pubtime":
            await callback.answer("Жду время в боте")
            await self.repository.set_pending_action(
                callback.from_user.id,
                "schedule_time",
                draft_id,
            )
            await self.bot.send_message(
                callback.from_user.id,
                f"Пришли время публикации для #{draft_id}.\n\n"
                "Примеры\n"
                "<code>18:30</code>\n"
                "<code>сегодня 18:30</code>\n"
                "<code>завтра 10:00</code>\n"
                "<code>25.10.2026 12:00</code>",
            )
            return
        if action == "pubunschedule":
            await callback.answer("Снимаю с расписания")
            await self.unschedule_draft(draft_id, callback.from_user.id)
            await self._refresh_plan_from_callback(callback)
            return
        if action == "pubback":
            await callback.answer("Возвращаю к черновику")
            if callback.message:
                await self.bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=self._editor_keyboard(draft),
                )
            return

        if action == "planitem":
            await callback.answer(f"Открываю пост #{draft_id}")
            if callback.message:
                await self._show_plan_item(
                    callback.message.chat.id,
                    callback.message.message_id,
                    draft,
                )
            return
        if action == "planreschedule":
            await callback.answer("Выбери новое время")
            occupied = await self.repository.scheduled_publication_times()
            current_time = self._draft_scheduled_at(draft)
            if current_time:
                occupied = [
                    value
                    for value in occupied
                    if int(value.timestamp() // 60)
                    != int(current_time.timestamp() // 60)
                ]
            slots = upcoming_publish_slots(
                self.settings.publish_schedule_slots,
                occupied,
                utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
            )
            keyboard = self._publish_options_keyboard(
                draft_id,
                slots=slots,
                utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
                back_callback=f"planitem:{draft_id}",
            )
            if callback.message:
                await self.bot.edit_message_reply_markup(
                    chat_id=callback.message.chat.id,
                    message_id=callback.message.message_id,
                    reply_markup=keyboard,
                )
            return

        if action == "title":
            await callback.answer("Подбираю новый заголовок")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "title",
                "Заголовок обновлен.",
            )
            await self._refresh_plan_from_callback(callback)
            return
        elif action == "tags":
            await callback.answer("Подбираю новые хэштеги")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "hashtags",
                "Хэштеги обновлены.",
            )
            await self._refresh_plan_from_callback(callback)
            return
        elif action in {"preview", "media", "image"}:
            await callback.answer("Подбираю другое изображение")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "image",
                "Изображение обновлено.",
            )
            await self._refresh_plan_from_callback(callback)
            return
        elif action == "sources":
            await self._send_sources(callback.from_user.id, draft)
        elif action == "regen":
            await callback.answer("Переписываю материал")
            if await self.pipeline.regenerate(draft_id):
                await self.refresh_draft(draft_id)
                await self.bot.send_message(callback.from_user.id, f"Черновик #{draft_id} переписан.")
            else:
                await self.bot.send_message(callback.from_user.id, "Не удалось переписать материал.")
            await self._refresh_plan_from_callback(callback)
            return
        elif action == "publish":
            await callback.answer("Выбери время публикации")
            occupied = await self.repository.scheduled_publication_times()
            slots = upcoming_publish_slots(
                self.settings.publish_schedule_slots,
                occupied,
                utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
            )
            keyboard = self._publish_options_keyboard(
                draft_id,
                slots=slots,
                utc_offset_hours=self.settings.publish_schedule_utc_offset_hours,
            )
            if callback.message:
                try:
                    await self.bot.edit_message_reply_markup(
                        chat_id=callback.message.chat.id,
                        message_id=callback.message.message_id,
                        reply_markup=keyboard,
                    )
                except TelegramBadRequest:
                    logger.exception(
                        "Could not show publication menu in editor channel for draft %s",
                        draft_id,
                    )
            await self.bot.send_message(
                callback.from_user.id,
                f"Когда опубликовать черновик #{draft_id}?",
                reply_markup=keyboard,
            )
            return
        elif action == "skip":
            await self.repository.update_draft(draft_id, status="rejected")
            if draft["editor_chat_id"] and draft["editor_message_id"]:
                await self.bot.edit_message_reply_markup(
                    chat_id=draft["editor_chat_id"],
                    message_id=draft["editor_message_id"],
                    reply_markup=None,
                )
            await self.bot.send_message(callback.from_user.id, f"Черновик #{draft_id} отклонен.")
        await callback.answer()

    async def _run_automatic_edit(
        self,
        user_id: int,
        draft_id: int,
        component: str,
        success_message: str,
    ) -> None:
        try:
            if component == "image":
                changed = await self.pipeline.regenerate_media(draft_id)
            else:
                changed = await self.pipeline.regenerate_component(draft_id, component)
            if not changed:
                await self.bot.send_message(
                    user_id,
                    f"Не удалось подобрать другой вариант для #{draft_id}.",
                )
                return
            await self.refresh_draft(
                draft_id,
                replace_message=component == "image",
            )
        except Exception:
            logger.exception(
                "Automatic %s regeneration failed for draft %s",
                component,
                draft_id,
            )
            await self.bot.send_message(
                user_id,
                f"Не удалось обновить черновик #{draft_id}. Попробуй еще раз.",
            )
            return
        await self.bot.send_message(
            user_id,
            f"Черновик #{draft_id}. {success_message}",
        )

    async def _refresh_plan_from_callback(self, callback: CallbackQuery) -> None:
        if not callback.message:
            return
        text = str(getattr(callback.message, "text", "") or "")
        if not text.startswith("Контент-план"):
            return
        await self._show_content_plan(
            callback.message.chat.id,
            callback.message.message_id,
        )

    async def deliver_draft(self, draft_id: int) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return
        editor_chat_id = await self.repository.get_setting("editor_chat_id")
        if not editor_chat_id:
            for owner_id in await self.repository.owner_ids():
                await self.bot.send_message(
                    owner_id,
                    f"Черновик #{draft_id} готов, но редакторский канал не привязан. Используй /setup.",
                )
            return
        sources = [SourceDocument.from_dict(item) for item in draft["sources"]]
        post = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        rendered = render_post(post, sources, self.settings.max_post_chars)
        preview_url = self._preview_url(draft)
        message, media_message_id = await self._send_editor_message(
            int(editor_chat_id),
            draft,
            rendered.html,
            rendered.rich_html,
            rendered.char_count,
            preview_url,
        )
        await self.repository.update_draft(
            draft_id,
            editor_chat_id=int(editor_chat_id),
            editor_message_id=message.message_id,
            editor_media_message_id=media_message_id,
        )

    async def refresh_draft(
        self,
        draft_id: int,
        *,
        replace_message: bool = False,
    ) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft or not draft["editor_chat_id"] or not draft["editor_message_id"]:
            return
        sources = [SourceDocument.from_dict(item) for item in draft["sources"]]
        post = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        rendered = render_post(post, sources, self.settings.max_post_chars)
        old_message_id = int(draft["editor_message_id"])
        old_media_message_id = draft.get("editor_media_message_id")
        media = self._selected_media(draft)
        file_value = await self._media_input(media)
        if file_value and not replace_message:
            try:
                edited = await self.bot.edit_message_text(
                    text=None,
                    chat_id=int(draft["editor_chat_id"]),
                    message_id=old_message_id,
                    parse_mode=None,
                    link_preview_options=None,
                    rich_message=self._rich_message(
                        rendered.rich_html,
                        file_value,
                        str(media.get("type") or "photo"),
                    ),
                    reply_markup=self._editor_keyboard(draft),
                )
                if isinstance(edited, Message):
                    if old_media_message_id and int(old_media_message_id) != old_message_id:
                        try:
                            await self.bot.delete_message(
                                chat_id=draft["editor_chat_id"],
                                message_id=int(old_media_message_id),
                            )
                        except TelegramBadRequest:
                            logger.warning(
                                "Could not remove legacy media message for draft %s",
                                draft_id,
                            )
                    await self.repository.update_draft(
                        draft_id,
                        editor_media_message_id=None,
                    )
                    return
            except TelegramBadRequest:
                logger.info(
                    "In-place rich edit failed for draft %s; replacing the message",
                    draft_id,
                )
        message, media_message_id = await self._send_editor_message(
            int(draft["editor_chat_id"]),
            draft,
            rendered.html,
            rendered.rich_html,
            rendered.char_count,
            self._preview_url(draft),
        )
        await self.repository.update_draft(
            draft_id,
            editor_message_id=message.message_id,
            editor_media_message_id=media_message_id,
        )
        try:
            await self.bot.delete_message(
                chat_id=draft["editor_chat_id"],
                message_id=old_message_id,
            )
            if old_media_message_id and int(old_media_message_id) != old_message_id:
                await self.bot.delete_message(
                    chat_id=draft["editor_chat_id"],
                    message_id=int(old_media_message_id),
                )
        except TelegramBadRequest:
            logger.warning("Could not remove previous editor message for draft %s", draft_id)

    async def _send_editor_message(
        self,
        chat_id: int,
        draft: dict[str, Any],
        text: str,
        rich_html: str,
        char_count: int,
        preview_url: str | None,
    ) -> tuple[Message, int | None]:
        media = self._selected_media(draft)
        file_value = await self._media_input(media)
        if file_value:
            try:
                message = await self.bot.send_rich_message(
                    chat_id=chat_id,
                    rich_message=self._rich_message(
                        rich_html,
                        file_value,
                        str(media.get("type") or "photo"),
                    ),
                    reply_markup=self._editor_keyboard(draft),
                )
                return message, None
            except TelegramBadRequest:
                logger.warning(
                    "Rich media delivery failed for draft %s",
                    draft["id"],
                )
                if char_count <= 1024:
                    if media.get("type") == "video":
                        message = await self.bot.send_video(
                            chat_id,
                            video=file_value,
                            caption=text,
                            reply_markup=self._editor_keyboard(draft),
                        )
                    else:
                        message = await self.bot.send_photo(
                            chat_id,
                            photo=file_value,
                            caption=text,
                            reply_markup=self._editor_keyboard(draft),
                        )
                    return message, None
                if not preview_url:
                    raise
        if not preview_url:
            raise RuntimeError(f"Draft {draft['id']} has neither downloadable media nor preview")
        message = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=self._editor_keyboard(draft),
            link_preview_options=self._preview_options(preview_url),
        )
        return message, None

    async def schedule_draft(
        self,
        draft_id: int,
        owner_id: int,
        scheduled_at: datetime,
    ) -> bool:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            await self.bot.send_message(owner_id, "Черновик не найден.")
            return False
        if draft["status"] == "published":
            await self.bot.send_message(owner_id, "Этот материал уже опубликован.")
            return False
        if not await self.repository.get_setting("target_chat_id"):
            await self.bot.send_message(
                owner_id,
                "Канал публикации не настроен. Используй /setup.",
            )
            return False
        scheduled_at = scheduled_at.astimezone(UTC)
        if scheduled_at <= datetime.now(UTC):
            await self.bot.send_message(
                owner_id,
                "Время публикации должно быть в будущем.",
            )
            return False
        await self.repository.update_draft(
            draft_id,
            status="scheduled",
            scheduled_at=scheduled_at.isoformat(),
            schedule_attempts=0,
            schedule_error=None,
        )
        await self._set_editor_keyboard(draft_id)
        self.schedule_wakeup.set()
        formatted = format_publish_time(
            scheduled_at,
            self.settings.publish_schedule_utc_offset_hours,
        )
        await self.bot.send_message(
            owner_id,
            f"Черновик #{draft_id} запланирован на {formatted}.",
        )
        return True

    async def unschedule_draft(self, draft_id: int, owner_id: int) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft or draft["status"] != "scheduled":
            await self.bot.send_message(owner_id, "Черновик не стоит в расписании.")
            return
        await self.repository.update_draft(
            draft_id,
            status="draft",
            scheduled_at=None,
            schedule_attempts=0,
            schedule_error=None,
        )
        await self._set_editor_keyboard(draft_id)
        self.schedule_wakeup.set()
        await self.bot.send_message(owner_id, f"Черновик #{draft_id} снят с расписания.")

    async def _set_editor_keyboard(self, draft_id: int) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft or not draft["editor_chat_id"] or not draft["editor_message_id"]:
            return
        try:
            await self.bot.edit_message_reply_markup(
                chat_id=int(draft["editor_chat_id"]),
                message_id=int(draft["editor_message_id"]),
                reply_markup=self._editor_keyboard(draft),
            )
        except TelegramBadRequest:
            logger.warning("Could not update schedule keyboard for draft %s", draft_id)

    async def scheduled_publish_worker(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            self.schedule_wakeup.clear()
            now = datetime.now(UTC)
            due = await self.repository.due_scheduled_drafts(now)
            for draft in due:
                draft_id = int(draft["id"])
                owners = await self.repository.owner_ids()
                owner_id = owners[0] if owners else None
                try:
                    published = await self.publish_draft(draft_id, owner_id)
                except Exception as error:
                    logger.exception("Scheduled publication failed for draft %s", draft_id)
                    published = False
                    failure = f"{type(error).__name__}: {error}"
                else:
                    failure = "Telegram publication failed"
                if published:
                    continue
                current = await self.repository.get_draft(draft_id)
                if not current or current["status"] != "scheduled":
                    continue
                attempts = int(current.get("schedule_attempts") or 0) + 1
                if attempts >= MAX_SCHEDULE_ATTEMPTS:
                    await self.repository.update_draft(
                        draft_id,
                        status="draft",
                        scheduled_at=None,
                        schedule_attempts=attempts,
                        schedule_error=failure,
                    )
                    await self._set_editor_keyboard(draft_id)
                    if owner_id is not None:
                        await self.bot.send_message(
                            owner_id,
                            f"Отложенная публикация #{draft_id} не удалась "
                            "после трёх попыток. Черновик вернут на редактуру.",
                        )
                else:
                    retry_at = datetime.now(UTC) + SCHEDULE_RETRY_DELAY
                    await self.repository.update_draft(
                        draft_id,
                        scheduled_at=retry_at.isoformat(),
                        schedule_attempts=attempts,
                        schedule_error=failure,
                    )
                    if owner_id is not None:
                        await self.bot.send_message(
                            owner_id,
                            f"Публикация #{draft_id} не прошла. "
                            "Повторю через 5 минут.",
                        )

            next_at = await self.repository.next_scheduled_publication()
            timeout = 3600.0
            if next_at is not None:
                timeout = max(
                    1.0,
                    min(timeout, (next_at - datetime.now(UTC)).total_seconds()),
                )
            stop_task = asyncio.create_task(stop_event.wait())
            wake_task = asyncio.create_task(self.schedule_wakeup.wait())
            done, pending = await asyncio.wait(
                {stop_task, wake_task},
                timeout=timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*pending, return_exceptions=True)
            if stop_task in done and stop_event.is_set():
                return

    async def publish_draft(self, draft_id: int, owner_id: int | None) -> bool:
        lock = self._publication_locks.setdefault(draft_id, asyncio.Lock())
        async with lock:
            return await self._publish_draft_unlocked(draft_id, owner_id)

    async def _publish_draft_unlocked(
        self,
        draft_id: int,
        owner_id: int | None,
    ) -> bool:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return False
        if draft["status"] == "published":
            if owner_id is not None:
                await self.bot.send_message(owner_id, "Этот материал уже опубликован.")
            return True
        target_chat_id = await self.repository.get_setting("target_chat_id")
        if not target_chat_id:
            if owner_id is not None:
                await self.bot.send_message(
                    owner_id,
                    "Канал публикации не настроен. Используй /setup.",
                )
            return False
        sources = [SourceDocument.from_dict(item) for item in draft["sources"]]
        post = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        rendered = render_post(post, sources, self.settings.max_post_chars)
        media = self._selected_media(draft)
        if not media:
            if owner_id is not None:
                await self.bot.send_message(
                    owner_id,
                    "Публикация остановлена. У материала нет изображения.",
                )
            return False
        file_value = await self._media_input(media)
        if not file_value:
            if owner_id is not None:
                await self.bot.send_message(
                    owner_id,
                    "Публикация остановлена. Не удалось скачать изображение.",
                )
            return False

        try:
            sent = await self.bot.send_rich_message(
                chat_id=int(target_chat_id),
                rich_message=self._rich_message(
                    rendered.rich_html,
                    file_value,
                    str(media.get("type") or "photo"),
                ),
            )
        except TelegramBadRequest:
            if rendered.char_count <= 1024:
                try:
                    if media.get("type") == "video":
                        sent = await self.bot.send_video(
                            int(target_chat_id),
                            video=file_value,
                            caption=rendered.html,
                        )
                    else:
                        sent = await self.bot.send_photo(
                            int(target_chat_id),
                            photo=file_value,
                            caption=rendered.html,
                        )
                except TelegramBadRequest:
                    sent = None
            else:
                sent = None
            if sent is None:
                logger.warning("Rich media publication failed for draft %s", draft_id)
                if owner_id is not None:
                    await self.bot.send_message(
                        owner_id,
                        "Публикация остановлена. Telegram не принял единый пост с изображением.",
                    )
                return False

        await self.repository.update_draft(
            draft_id,
            status="published",
            target_chat_id=int(target_chat_id),
            target_message_id=sent.message_id,
            target_media_message_id=None,
            scheduled_at=None,
            schedule_attempts=0,
            schedule_error=None,
        )
        if draft["editor_chat_id"] and draft["editor_message_id"]:
            await self.bot.edit_message_reply_markup(
                chat_id=draft["editor_chat_id"],
                message_id=draft["editor_message_id"],
                reply_markup=None,
            )
        if owner_id is not None:
            await self.bot.send_message(owner_id, f"Черновик #{draft_id} опубликован.")
        return True

    async def _send_sources(self, user_id: int, draft: dict[str, Any]) -> None:
        lines = [f"Источники черновика #{draft['id']}\n"]
        linkable_sources = [
            (index, source)
            for index, source in enumerate(draft["sources"])
            if source.get("linkable")
        ]
        for index, source in linkable_sources[:12]:
            title = html.escape(source.get("title") or source.get("publisher") or "Без названия")
            url = html.escape(source.get("url") or "")
            marker = "Основной" if index == draft["triage"].get("primary_source_index") else str(index)
            if url:
                lines.append(f"{marker}  <a href=\"{url}\">{title}</a>")
            else:
                lines.append(f"{marker}  {title}")
        if not linkable_sources:
            lines.append("Официальные первоисточники не найдены.")
        text = "\n".join(lines)
        await self.bot.send_message(user_id, text[:4000], link_preview_options=LinkPreviewOptions(is_disabled=True))

    @classmethod
    def _editor_keyboard(cls, draft: dict[str, Any]) -> InlineKeyboardMarkup:
        draft_id = int(draft["id"])
        if draft.get("status") == "scheduled":
            return cls._scheduled_keyboard(draft_id)
        return cls._draft_keyboard(draft_id)

    @staticmethod
    def _publish_options_keyboard(
        draft_id: int,
        *,
        slots: list[datetime] | None = None,
        utc_offset_hours: int = 4,
        now: datetime | None = None,
        back_callback: str | None = None,
    ) -> InlineKeyboardMarkup:
        rows: list[list[InlineKeyboardButton]] = [
            [
                InlineKeyboardButton(
                    text="Сейчас",
                    callback_data=f"pubnow:{draft_id}",
                ),
                InlineKeyboardButton(
                    text="Следующее окно",
                    callback_data=f"pubnext:{draft_id}",
                ),
            ],
        ]
        slot_buttons = [
            InlineKeyboardButton(
                text=publish_slot_label(
                    value,
                    utc_offset_hours=utc_offset_hours,
                    now=now,
                ),
                callback_data=f"pubslot:{draft_id}:{int(value.timestamp())}",
            )
            for value in slots or []
        ]
        rows.extend(
            slot_buttons[index : index + 2]
            for index in range(0, len(slot_buttons), 2)
        )
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        text="Указать другое время",
                        callback_data=f"pubtime:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Назад",
                        callback_data=back_callback or f"pubback:{draft_id}",
                    ),
                ],
            ]
        )
        return InlineKeyboardMarkup(
            inline_keyboard=rows,
        )

    @staticmethod
    def _scheduled_keyboard(draft_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Опубликовать сейчас",
                        callback_data=f"pubnow:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Изменить время",
                        callback_data=f"publish:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Снять с расписания",
                        callback_data=f"pubunschedule:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Контент-план",
                        callback_data="plan:0",
                    ),
                    InlineKeyboardButton(
                        text="Канал публикации",
                        callback_data="targetmenu:0",
                    ),
                ],
            ]
        )

    @staticmethod
    def _draft_keyboard(draft_id: int) -> InlineKeyboardMarkup:
        return InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(text="Опубликовать", callback_data=f"publish:{draft_id}"),
                    InlineKeyboardButton(text="Отклонить", callback_data=f"skip:{draft_id}"),
                ],
                [
                    InlineKeyboardButton(
                        text="Другой заголовок",
                        callback_data=f"title:{draft_id}",
                    ),
                    InlineKeyboardButton(
                        text="Другие хэштеги",
                        callback_data=f"tags:{draft_id}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Другое изображение",
                        callback_data=f"image:{draft_id}",
                    ),
                    InlineKeyboardButton(text="Переписать всё", callback_data=f"regen:{draft_id}"),
                ],
                [
                    InlineKeyboardButton(text="Источники", callback_data=f"sources:{draft_id}"),
                ],
                [
                    InlineKeyboardButton(
                        text="Контент-план",
                        callback_data="plan:0",
                    ),
                    InlineKeyboardButton(
                        text="Канал публикации",
                        callback_data="targetmenu:0",
                    ),
                ],
            ]
        )

    @staticmethod
    def _selected_media(draft: dict[str, Any]) -> dict[str, Any] | None:
        media = draft["media"]
        if not media:
            return None
        return media[int(draft["preview_index"]) % len(media)]

    def _preview_url(self, draft: dict[str, Any]) -> str | None:
        media = self._selected_media(draft)
        if media and str(media.get("page_url", "")).startswith("http"):
            source_index = media.get("source_index")
            if (
                isinstance(source_index, int)
                and source_index < len(draft["sources"])
                and draft["sources"][source_index].get("linkable")
            ):
                return str(media["page_url"])
        for source in draft["sources"]:
            if (
                source.get("linkable")
                and source.get("og_image")
                and str(source.get("url", "")).startswith("http")
            ):
                return str(source["url"])
        return None

    async def _media_input(self, media: dict[str, Any] | None) -> str | BufferedInputFile | None:
        if not media:
            return None
        if media.get("telegram_file_id"):
            return str(media["telegram_file_id"])
        image_url = str(media.get("image_url") or "")
        if not image_url:
            return None
        downloaded = await self.pipeline.extractor.download_image(image_url)
        if not downloaded:
            return None
        content, filename = downloaded
        return BufferedInputFile(content, filename=filename)

    @staticmethod
    def _rich_message(
        rich_html: str,
        file_value: str | BufferedInputFile,
        media_type: str,
    ) -> InputRichMessage:
        media_id = "cover"
        if media_type == "video":
            tag = f'<video src="tg://video?id={media_id}"></video>'
            input_media = InputMediaVideo(media=file_value, parse_mode=None)
        else:
            tag = f'<img src="tg://photo?id={media_id}"/>'
            input_media = InputMediaPhoto(media=file_value, parse_mode=None)
        return InputRichMessage(
            html=f"{tag}{rich_html}",
            media=[InputRichMessageMedia(id=media_id, media=input_media)],
        )

    @staticmethod
    def _preview_options(url: str | None) -> LinkPreviewOptions:
        if not url:
            return LinkPreviewOptions(is_disabled=True)
        return LinkPreviewOptions(
            url=url,
            prefer_large_media=True,
            show_above_text=True,
        )

    @staticmethod
    def _candidate_from_message(message: Message) -> StoryCandidate:
        text = (message.text or message.caption or "").strip()
        urls = URL_RE.findall(text)
        for entity in list(message.entities or []) + list(message.caption_entities or []):
            if entity.url:
                urls.append(entity.url)

        origin_ref = ""
        origin = message.forward_origin
        if origin and hasattr(origin, "chat") and hasattr(origin, "message_id"):
            chat = origin.chat
            if getattr(chat, "username", None):
                origin_ref = f"https://t.me/{chat.username}/{origin.message_id}"
                urls.insert(0, origin_ref)

        file_id = None
        media_type = None
        if message.photo:
            file_id = message.photo[-1].file_id
            media_type = "photo"
        elif message.video:
            file_id = message.video.file_id
            media_type = "video"

        title = text.splitlines()[0][:240] if text else "Пересланный материал"
        return StoryCandidate(
            origin_type="telegram_forward",
            origin_ref=origin_ref,
            title=title,
            text=text,
            urls=list(dict.fromkeys(urls)),
            telegram_file_id=file_id,
            telegram_media_type=media_type,
        )
