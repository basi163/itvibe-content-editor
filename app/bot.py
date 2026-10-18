from __future__ import annotations

import asyncio
import html
import json
import logging
import re
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
        self.dispatcher.include_router(self.router)
        self._register_handlers()

    async def initialize(self) -> None:
        await self.repository.clear_pending_actions()
        await self.bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить редактор"),
                BotCommand(command="setup", description="Привязать каналы"),
                BotCommand(command="status", description="Показать состояние системы"),
                BotCommand(command="sources", description="Показать источники"),
                BotCommand(command="settings", description="Настройки генерации"),
                BotCommand(command="help", description="Помощь"),
            ]
        )
        if self.settings.editor_chat_id:
            await self.repository.set_setting("editor_chat_id", str(self.settings.editor_chat_id))
        if self.settings.target_chat_id:
            await self.repository.set_setting("target_chat_id", str(self.settings.target_chat_id))

    async def start_polling(self) -> None:
        await self.dispatcher.start_polling(self.bot, allowed_updates=self.dispatcher.resolve_used_update_types())

    async def close(self) -> None:
        await self.bot.session.close()

    def _register_handlers(self) -> None:
        self.router.message.register(self._start, Command("start"))
        self.router.message.register(self._setup, Command("setup"))
        self.router.message.register(self._status, Command("status"))
        self.router.message.register(self._sources, Command("sources"))
        self.router.message.register(self._settings, Command("settings"))
        self.router.message.register(self._help, Command("help"))
        self.router.message.register(self._chat_shared, F.chat_shared)
        self.router.callback_query.register(self._callback, F.data)
        self.router.message.register(self._private_message, F.chat.type == ChatType.PRIVATE)

    async def _is_owner(self, user_id: int) -> bool:
        return await self.repository.is_owner(user_id)

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
        keyboard = ReplyKeyboardMarkup(
            keyboard=[
                [
                    KeyboardButton(
                        text="Выбрать редакторский канал",
                        request_chat=KeyboardButtonRequestChat(
                            request_id=EDITOR_REQUEST_ID,
                            chat_is_channel=True,
                            bot_is_member=True,
                        ),
                    )
                ],
                [
                    KeyboardButton(
                        text="Выбрать канал публикации",
                        request_chat=KeyboardButtonRequestChat(
                            request_id=TARGET_REQUEST_ID,
                            chat_is_channel=True,
                            bot_is_member=True,
                        ),
                    )
                ],
            ],
            resize_keyboard=True,
            one_time_keyboard=True,
        )
        await message.answer(
            "Сначала добавь бота администратором нужного канала, затем выбери канал.",
            reply_markup=keyboard,
        )

    async def _chat_shared(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        shared: ChatShared = message.chat_shared
        try:
            me = await self.bot.get_me()
            member = await self.bot.get_chat_member(shared.chat_id, me.id)
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
            await message.answer(
                "Канал публикации привязан.",
                reply_markup=ReplyKeyboardRemove(),
            )

    async def _status(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        stats = await self.repository.stats()
        editor = await self.repository.get_setting("editor_chat_id") or "не привязан"
        target = await self.repository.get_setting("target_chat_id") or "не привязан"
        await message.answer(
            "Сервис работает.\n\n"
            f"Получено материалов  {stats.get('feed_items', 0)}\n"
            f"Создано черновиков  {stats.get('drafts', 0)}\n"
            f"Опубликовано  {stats.get('drafts_published', 0)}\n"
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
        await message.answer(
            f"Лимит поста  {self.settings.max_post_chars}\n"
            f"Порог релевантности  {self.settings.relevance_threshold:.2f}\n"
            "Проверка RSS  адаптивно\n"
            f"В активные часы  каждые {self.settings.rss_poll_interval_seconds} секунд\n"
            "Вне активных часов  каждые "
            f"{self.settings.rss_offpeak_poll_interval_seconds} секунд\n"
            f"Модель отбора  <code>{html.escape(self.settings.triage_model)}</code>\n"
            f"Модель текста  <code>{html.escape(self.settings.writer_model)}</code>"
        )

    async def _help(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        await message.answer(
            "Перешли сюда пост из любого канала или дождись новой публикации Solidot. "
            "Подготовленный материал появится в редакторском канале. Управление выполняется "
            "кнопками под черновиком."
        )

    async def _private_message(self, message: Message) -> None:
        if not message.from_user or not await self._is_owner(message.from_user.id):
            return
        if message.text and message.text.startswith("/"):
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
            await self.refresh_draft(draft_id)
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
        action, _, value = (callback.data or "").partition(":")
        if not value.isdigit():
            await callback.answer("Некорректная команда", show_alert=True)
            return
        draft_id = int(value)
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            await callback.answer("Черновик не найден", show_alert=True)
            return

        if action == "title":
            await callback.answer("Подбираю новый заголовок")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "title",
                "Заголовок обновлен.",
            )
            return
        elif action == "tags":
            await callback.answer("Подбираю новые хэштеги")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "hashtags",
                "Хэштеги обновлены.",
            )
            return
        elif action in {"preview", "media", "image"}:
            await callback.answer("Подбираю другое изображение")
            await self._run_automatic_edit(
                callback.from_user.id,
                draft_id,
                "image",
                "Изображение обновлено.",
            )
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
            return
        elif action == "publish":
            await callback.answer("Публикую")
            await self.publish_draft(draft_id, callback.from_user.id)
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
            await self.refresh_draft(draft_id)
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

    async def refresh_draft(self, draft_id: int) -> None:
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
        if file_value:
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
                    reply_markup=self._draft_keyboard(draft_id),
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
                    reply_markup=self._draft_keyboard(int(draft["id"])),
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
                            reply_markup=self._draft_keyboard(int(draft["id"])),
                        )
                    else:
                        message = await self.bot.send_photo(
                            chat_id,
                            photo=file_value,
                            caption=text,
                            reply_markup=self._draft_keyboard(int(draft["id"])),
                        )
                    return message, None
                if not preview_url:
                    raise
        if not preview_url:
            raise RuntimeError(f"Draft {draft['id']} has neither downloadable media nor preview")
        message = await self.bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=self._draft_keyboard(int(draft["id"])),
            link_preview_options=self._preview_options(preview_url),
        )
        return message, None

    async def publish_draft(self, draft_id: int, owner_id: int) -> None:
        draft = await self.repository.get_draft(draft_id)
        if not draft:
            return
        if draft["status"] == "published":
            await self.bot.send_message(owner_id, "Этот материал уже опубликован.")
            return
        target_chat_id = await self.repository.get_setting("target_chat_id")
        if not target_chat_id:
            await self.bot.send_message(owner_id, "Канал публикации не настроен. Используй /setup.")
            return
        sources = [SourceDocument.from_dict(item) for item in draft["sources"]]
        post = GeneratedPost(draft["title"], draft["body"], draft["hashtags"])
        rendered = render_post(post, sources, self.settings.max_post_chars)
        media = self._selected_media(draft)
        if not media:
            await self.bot.send_message(owner_id, "Публикация остановлена. У материала нет изображения.")
            return
        file_value = await self._media_input(media)
        if not file_value:
            await self.bot.send_message(
                owner_id,
                "Публикация остановлена. Не удалось скачать изображение.",
            )
            return

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
                await self.bot.send_message(
                    owner_id,
                    "Публикация остановлена. Telegram не принял единый пост с изображением.",
                )
                return

        await self.repository.update_draft(
            draft_id,
            status="published",
            target_chat_id=int(target_chat_id),
            target_message_id=sent.message_id,
            target_media_message_id=None,
        )
        if draft["editor_chat_id"] and draft["editor_message_id"]:
            await self.bot.edit_message_reply_markup(
                chat_id=draft["editor_chat_id"],
                message_id=draft["editor_message_id"],
                reply_markup=None,
            )
        await self.bot.send_message(owner_id, f"Черновик #{draft_id} опубликован.")

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
