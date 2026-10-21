from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
from bs4 import BeautifulSoup, Tag

from app.db import Repository
from app.extractor import is_telegram_url, normalize_url
from app.models import StoryCandidate
from app.pipeline import ContentPipeline
from app.rss import FeedEntry

logger = logging.getLogger(__name__)
USERNAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{4,31}$")
STYLE_URL_RE = re.compile(r"url\((?:'|\")?([^)'\"]+)", re.IGNORECASE)
RETRY_DELAY = timedelta(minutes=30)
MAX_ATTEMPTS = 3


def parse_public_channel_username(value: str) -> str:
    cleaned = value.strip().rstrip("/")
    if cleaned.startswith("@"):
        cleaned = cleaned[1:]
    elif "://" in cleaned:
        parsed = urlparse(cleaned)
        host = (parsed.hostname or "").casefold().removeprefix("www.")
        if host not in {"t.me", "telegram.me", "telegram.dog"}:
            raise ValueError("Нужен @username или публичная ссылка t.me")
        parts = [part for part in parsed.path.split("/") if part]
        if parts and parts[0].casefold() == "s":
            parts = parts[1:]
        if not parts:
            raise ValueError("В ссылке не найден username канала")
        cleaned = parts[0]
    if not USERNAME_RE.fullmatch(cleaned):
        raise ValueError("Некорректный username публичного Telegram-канала")
    return cleaned


def _message_id(element: Tag, username: str) -> int | None:
    data_post = str(element.get("data-post") or "")
    channel, separator, raw_id = data_post.rpartition("/")
    if not separator or channel.casefold() != username.casefold() or not raw_id.isdigit():
        return None
    return int(raw_id)


def _media_urls(element: Tag, base_url: str) -> list[str]:
    urls: list[str] = []
    for media in element.select(
        ".tgme_widget_message_photo_wrap, .tgme_widget_message_video_player, "
        ".tgme_widget_message_video_thumb"
    ):
        style = str(media.get("style") or "")
        match = STYLE_URL_RE.search(style)
        if match:
            urls.append(html.unescape(match.group(1)))
    for media in element.select("img[src], video[poster]"):
        value = media.get("src") or media.get("poster")
        if value:
            urls.append(str(value))
    normalized: list[str] = []
    for value in urls:
        candidate = normalize_url(urljoin(base_url, value))
        if candidate and candidate not in normalized:
            normalized.append(candidate)
    return normalized


@dataclass(frozen=True)
class TelegramChannelSnapshot:
    username: str
    title: str
    url: str
    entries: list[FeedEntry]

    @property
    def latest_id(self) -> str | None:
        if not self.entries:
            return None
        return str(max(int(entry.external_id) for entry in self.entries))


class TelegramPublicFeed:
    def __init__(self, username: str, timeout: float) -> None:
        self.username = parse_public_channel_username(username)
        self.timeout = timeout
        self.url = f"https://t.me/s/{self.username}"

    async def fetch(self) -> TelegramChannelSnapshot:
        async with httpx.AsyncClient(
            timeout=self.timeout,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "Chrome/126 Safari/537.36 ITVibeContentEditor/0.1"
                )
            },
        ) as client:
            response = await client.get(self.url)
            response.raise_for_status()
        final_host = (response.url.host or "").casefold().removeprefix("www.")
        if final_host != "t.me":
            raise ValueError("Telegram перенаправил запрос за пределы t.me")
        return self.parse(response.text)

    def parse(self, html_text: str) -> TelegramChannelSnapshot:
        soup = BeautifulSoup(html_text, "html.parser")
        title_node = soup.select_one(".tgme_channel_info_header_title span")
        title = title_node.get_text(" ", strip=True) if title_node else ""
        messages = soup.select(".tgme_widget_message[data-post]")
        if not title and not messages:
            raise ValueError(
                "Публичная веб-лента недоступна: проверь username и настройки канала"
            )
        if not title:
            owner = soup.select_one(".tgme_widget_message_owner_name span")
            title = owner.get_text(" ", strip=True) if owner else f"@{self.username}"

        entries: dict[int, FeedEntry] = {}
        for message in messages:
            message_id = _message_id(message, self.username)
            if message_id is None:
                continue
            text_node = message.select_one(".tgme_widget_message_text")
            text = text_node.get_text("\n", strip=True) if text_node else ""
            media_urls = _media_urls(message, self.url)
            if not text:
                continue
            links: list[str] = []
            if text_node:
                for anchor in text_node.select("a[href]"):
                    candidate = normalize_url(urljoin(self.url, str(anchor.get("href"))))
                    if candidate and not is_telegram_url(candidate) and candidate not in links:
                        links.append(candidate)
            time_node = message.select_one("time[datetime]")
            published_at = str(time_node.get("datetime")) if time_node else None
            permalink = f"https://t.me/{self.username}/{message_id}"
            entry_title = next(
                (line.strip() for line in text.splitlines() if line.strip()),
                f"Публикация @{self.username}",
            )[:240]
            existing = entries.get(message_id)
            if existing:
                existing.raw["media_urls"] = list(
                    dict.fromkeys([*existing.raw.get("media_urls", []), *media_urls])
                )
                existing.urls = list(dict.fromkeys([*existing.urls, *links]))
                continue
            entries[message_id] = FeedEntry(
                external_id=str(message_id),
                title=entry_title,
                link=permalink,
                summary=text,
                published_at=published_at,
                urls=links,
                raw={
                    "id": str(message_id),
                    "channel_username": self.username,
                    "channel_title": title,
                    "link": permalink,
                    "summary": text,
                    "published_at": published_at,
                    "media_urls": media_urls,
                },
            )
        return TelegramChannelSnapshot(
            username=self.username,
            title=title,
            url=self.url,
            entries=sorted(entries.values(), key=lambda entry: int(entry.external_id)),
        )


class TelegramSourceManager:
    kind = "telegram_web"

    def __init__(
        self,
        repository: Repository,
        pipeline: ContentPipeline,
        *,
        timeout: float,
    ) -> None:
        self.repository = repository
        self.pipeline = pipeline
        self.timeout = timeout
        self._locks: dict[int, asyncio.Lock] = {}

    @staticmethod
    def source_key(username: str) -> str:
        return f"telegram_web:{username.casefold()}"

    async def add(self, value: str) -> tuple[dict[str, Any], bool]:
        username = parse_public_channel_username(value)
        source_key = self.source_key(username)
        existing = await self.repository.get_content_source_by_key(source_key)
        if existing:
            return existing, False
        snapshot = await TelegramPublicFeed(username, self.timeout).fetch()
        source_id = await self.repository.create_content_source(
            kind=self.kind,
            source_key=source_key,
            title=snapshot.title,
            url=snapshot.url,
            last_external_id=snapshot.latest_id,
        )
        source = await self.repository.get_content_source(source_id)
        if not source:
            raise RuntimeError("Не удалось сохранить Telegram-источник")
        return source, True

    async def poll_source(self, source: dict[str, Any]) -> dict[str, int]:
        source_id = int(source["id"])
        lock = self._locks.setdefault(source_id, asyncio.Lock())
        if lock.locked():
            return {"found": 0, "processed": 0, "drafts": 0}
        async with lock:
            checked_at = datetime.now(UTC).isoformat()
            try:
                username = str(source["source_key"]).split(":", 1)[1]
                snapshot = await TelegramPublicFeed(username, self.timeout).fetch()
                last_id = int(source.get("last_external_id") or 0)
                new_entries = [
                    entry for entry in snapshot.entries if int(entry.external_id) > last_id
                ]
                work: list[tuple[FeedEntry, int | None]] = []
                for entry in snapshot.entries:
                    state = await self.repository.get_feed_item_state(
                        str(source["source_key"]),
                        entry.external_id,
                    )
                    if int(entry.external_id) > last_id and state is None:
                        work.append((entry, None))
                        continue
                    if self._retryable(state):
                        work.append((entry, int(state["id"])))
                processed = 0
                drafts = 0
                for entry, existing_item_id in work:
                    if existing_item_id is None:
                        item_id = await self.repository.add_feed_item(
                            str(source["source_key"]),
                            entry.external_id,
                            entry.link,
                            entry.title,
                            entry.published_at,
                            "processing",
                            entry.raw,
                        )
                    else:
                        item_id = existing_item_id
                        await self.repository.mark_feed_processing(item_id)
                    candidate = StoryCandidate(
                        origin_type=self.kind,
                        origin_ref=entry.link,
                        title=entry.title,
                        text=entry.summary,
                        urls=entry.urls,
                        media_urls=list(entry.raw.get("media_urls", [])),
                    )
                    draft_id = await self.pipeline.process_feed_item(item_id, candidate)
                    processed += 1
                    drafts += int(draft_id is not None)
                    last_id = max(last_id, int(entry.external_id))
                    await self.repository.update_content_source(
                        source_id,
                        last_external_id=str(last_id),
                    )
                latest = max(
                    last_id,
                    int(snapshot.latest_id or 0),
                )
                await self.repository.update_content_source(
                    source_id,
                    title=snapshot.title,
                    url=snapshot.url,
                    last_external_id=str(latest) if latest else None,
                    last_checked_at=checked_at,
                    last_success_at=checked_at,
                    last_error=None,
                )
                return {
                    "found": len(new_entries),
                    "processed": processed,
                    "drafts": drafts,
                }
            except Exception as exc:
                logger.warning(
                    "Telegram public source %s check failed: %s",
                    source_id,
                    type(exc).__name__,
                )
                await self.repository.update_content_source(
                    source_id,
                    last_checked_at=checked_at,
                    last_error=f"{type(exc).__name__}: {' '.join(str(exc).split())}"[:500],
                )
                raise

    @staticmethod
    def _retryable(state: dict[str, Any] | None) -> bool:
        if not state or state.get("status") != "failed":
            return False
        if int(state.get("attempt_count") or 0) >= MAX_ATTEMPTS:
            return False
        try:
            updated_at = datetime.fromisoformat(str(state["updated_at"]))
        except (KeyError, TypeError, ValueError):
            return False
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=UTC)
        return datetime.now(UTC) - updated_at >= RETRY_DELAY

    async def poll_all(self) -> dict[str, int]:
        totals = {"sources": 0, "found": 0, "processed": 0, "drafts": 0, "errors": 0}
        sources = await self.repository.content_sources(
            kind=self.kind,
            enabled_only=True,
        )
        totals["sources"] = len(sources)
        for source in sources:
            try:
                result = await self.poll_source(source)
            except Exception:  # noqa: BLE001 - one source must not stop the others
                totals["errors"] += 1
                continue
            for key in ("found", "processed", "drafts"):
                totals[key] += result[key]
        return totals
